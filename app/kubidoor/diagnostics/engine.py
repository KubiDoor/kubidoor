"""Diagnostic engine: enriches findings and produces on-demand diagnostic reports."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from kubidoor.core.clock import Clock, utcnow
from kubidoor.core.config import MonitoringSettings
from kubidoor.core.exceptions import (
    KubernetesError,
    MetricsUnavailableError,
    NotFoundError,
)
from kubidoor.core.types import ResourceType
from kubidoor.diagnostics.rules import RULES, Diagnosis, DiagnosticContext
from kubidoor.incidents.models import Finding
from kubidoor.kubernetes.client import KubeGateway
from kubidoor.kubernetes.events import events_for
from kubidoor.kubernetes.models import EventInfo, NodeInfo, PodInfo
from kubidoor.monitoring.checks import check_deployment, check_node, check_pod, check_pvc

_KIND = {
    ResourceType.POD: "Pod",
    ResourceType.NODE: "Node",
    ResourceType.DEPLOYMENT: "Deployment",
    ResourceType.PVC: "PersistentVolumeClaim",
}


@dataclass(slots=True)
class DiagnosisEntry:
    problem_type: str
    severity: str
    title: str
    diagnosis: Diagnosis


@dataclass(slots=True)
class DiagnosticReport:
    resource_type: ResourceType
    namespace: str | None
    resource_name: str
    resource_uid: str | None
    healthy: bool
    state_facts: list[str]
    entries: list[DiagnosisEntry] = field(default_factory=list)
    metrics_available: bool = False
    metrics_note: str = ""
    generated_at: datetime | None = None


def diagnosis_to_evidence(d: Diagnosis) -> dict[str, Any]:
    return {
        "observed_facts": d.observed_facts,
        "probable_cause": d.probable_cause,
        "confidence": d.confidence,
        "recommended_actions": d.recommended_actions,
    }


class DiagnosticEngine:
    def __init__(
        self, gateway: KubeGateway, monitoring: MonitoringSettings, clock: Clock = utcnow
    ) -> None:
        self._gateway = gateway
        self._cfg = monitoring
        self._clock = clock

    # ---------------------------------------------------------------- monitoring enrichment --
    def enrich(self, finding: Finding, ctx: DiagnosticContext) -> Finding:
        """Attach probable cause / recommendations / structured evidence to a finding."""
        rule = RULES.get(finding.problem_type)
        if rule is None or finding.subject is None:
            return finding
        diagnosis = rule(finding, finding.subject, ctx)
        evidence = {**finding.evidence, "diagnosis": diagnosis_to_evidence(diagnosis)}
        return replace(
            finding,
            evidence=evidence,
            probable_cause=f"{diagnosis.probable_cause} (confidence: {diagnosis.confidence})",
            recommended_action="\n".join(
                f"{i}. {a}" for i, a in enumerate(diagnosis.recommended_actions, 1)
            ),
        )

    def context_for(
        self,
        finding: Finding,
        all_events: list[EventInfo],
        *,
        nodes: list[NodeInfo] | None,
        pods: list[PodInfo] | None,
        pod_usage: dict[tuple[str, str], dict[str, Any]] | None,
        usage_note: str,
    ) -> DiagnosticContext:
        ctx = DiagnosticContext(
            now=self._clock(),
            events=events_for(
                all_events, _KIND[finding.resource_type], finding.namespace, finding.resource_name
            ),
            nodes=nodes,
            pods=pods,
            usage_note=usage_note,
        )
        if pod_usage is not None and finding.resource_type == ResourceType.POD:
            ctx.pod_usage = pod_usage.get((finding.namespace or "", finding.resource_name))
        return ctx

    # ---------------------------------------------------------------------- on-demand report --
    def diagnose_resource(
        self, resource_type: ResourceType, namespace: str | None, name: str
    ) -> DiagnosticReport:
        """Read-only, live diagnosis of one resource. Raises NotFoundError / KubernetesError."""
        now = self._clock()
        gw = self._gateway
        # On-demand analysis ignores the time-based alerting thresholds: if something is wrong
        # right now, explain it, rather than staying silent because it is "too young to alert".
        cfg = self._cfg.model_copy(
            update={
                "pending_threshold_seconds": 0,
                "not_ready_threshold_seconds": 0,
                "deployment_grace_seconds": 0,
                "pvc_pending_threshold_seconds": 0,
            }
        )
        subject: Any
        events = gw.list_events_for(_KIND[resource_type], namespace, name)
        nodes: list[NodeInfo] | None = None
        pods: list[PodInfo] | None = None
        pod_usage: dict[tuple[str, str], dict[str, Any]] | None = None
        usage_note = "Resource metrics are not applicable to this resource"

        if resource_type == ResourceType.POD:
            subject = gw.get_pod(_req(namespace), name)
            _found(subject, resource_type, namespace, name)
            findings = check_pod(subject, now, cfg)
            nodes = _safe(gw.list_nodes)
            try:
                pod_usage = gw.pod_usage(namespace)
                usage_note = "metrics-server data available"
            except MetricsUnavailableError as exc:
                usage_note = exc.message
            facts = [
                f"Pod phase: {subject.phase}; ready: {subject.ready}; node: {subject.node_name}; "
                f"restarts: {subject.total_restarts}."
            ]
        elif resource_type == ResourceType.NODE:
            subject = gw.get_node(name)
            _found(subject, resource_type, namespace, name)
            findings = check_node(subject, now)
            facts = [
                f"Node Ready status: {subject.ready_status}; unschedulable: {subject.unschedulable}."
            ]
        elif resource_type == ResourceType.DEPLOYMENT:
            subject = gw.get_deployment(_req(namespace), name)
            _found(subject, resource_type, namespace, name)
            findings = check_deployment(subject, now, cfg)
            pods = _safe(lambda: gw.list_pods([_req(namespace)]))
            facts = [
                f"Replicas desired/ready/available: {subject.replicas}/"
                f"{subject.ready_replicas}/{subject.available_replicas}."
            ]
        else:
            subject = gw.get_pvc(_req(namespace), name)
            _found(subject, resource_type, namespace, name)
            findings = check_pvc(subject, now, cfg, events)
            facts = [f"PVC phase: {subject.phase}."]

        report = DiagnosticReport(
            resource_type=resource_type,
            namespace=namespace,
            resource_name=name,
            resource_uid=subject.uid,
            healthy=not findings,
            state_facts=facts,
            metrics_available=pod_usage is not None,
            metrics_note=usage_note,
            generated_at=now,
        )
        for f in findings:
            rule = RULES.get(f.problem_type)
            if rule is None:
                continue
            ctx = DiagnosticContext(
                now=now,
                events=events,
                nodes=nodes,
                pods=pods,
                usage_note=usage_note,
                pod_usage=(pod_usage or {}).get((namespace or "", name)) if pod_usage else None,
            )
            report.entries.append(
                DiagnosisEntry(f.problem_type, f.severity.value, f.title, rule(f, subject, ctx))
            )
        return report


def _req(namespace: str | None) -> str:
    if not namespace:
        raise NotFoundError("A namespace is required for this resource type")
    return namespace


def _found(obj: object | None, rtype: ResourceType, namespace: str | None, name: str) -> None:
    if obj is None:
        where = f"{namespace}/{name}" if namespace else name
        raise NotFoundError(f"{rtype.value} {where} not found")


def _safe(fn: Any) -> Any:
    """Optional enrichment: a failure here must not break the primary diagnosis."""
    try:
        return fn()
    except KubernetesError:
        return None
