"""Recovery engine: request -> approve -> execute -> verify, with every step policy-checked.

Workflow (matches the spec):
  DETECTED -> POLICY_VALIDATION -> APPROVAL_REQUIRED -> DRY_RUN | EXECUTION -> VERIFICATION
           -> SUCCEEDED | FAILED -> AUDIT_LOG

Concurrency safety
  Approve, reject and the execution *claim* are single atomic ``UPDATE ... WHERE status = <expected>``
  statements; exactly one caller can win each transition, so duplicate or concurrent requests
  cannot execute an action twice. Terminal transitions are only applied to RUNNING actions, so
  they are idempotent too.
"""

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from kubidoor.audit import service as audit_events
from kubidoor.audit.service import AuditService
from kubidoor.core.clock import Clock, utcnow
from kubidoor.core.config import RecoverySettings
from kubidoor.core.exceptions import (
    ConflictError,
    KubernetesError,
    NotFoundError,
    PolicyViolationError,
)
from kubidoor.core.types import ACTIVE_ACTION_STATUSES, ActionStatus, ActionType, ResourceType
from kubidoor.database.models import Incident, RecoveryAction
from kubidoor.database.session import SessionFactory, session_scope
from kubidoor.incidents.service import IncidentService
from kubidoor.kubernetes.client import KubeGateway
from kubidoor.logging.setup import get_logger, log_context, redact
from kubidoor.metrics.prometheus import Metrics
from kubidoor.recovery import actions as action_ops
from kubidoor.recovery import policies, verification

logger = get_logger("recovery")

# Which action (if any) is *proposed* automatically for a detected problem.
SUGGESTED_ACTIONS: dict[str, ActionType] = {
    "CrashLoopBackOff": ActionType.POD_DELETE,
    "PodFailed": ActionType.POD_DELETE,
    "PodUnknown": ActionType.POD_DELETE,
    "ReplicasUnavailable": ActionType.DEPLOYMENT_RESTART,
    "ProgressDeadlineExceeded": ActionType.DEPLOYMENT_RESTART,
}

MONITOR = "monitor"
SYSTEM = "system"
_HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class _Target:
    action_id: str
    incident_id: str
    action_type: ActionType
    resource_type: ResourceType
    namespace: str
    name: str
    uid: str | None
    approved_by: str | None
    dry_run: bool
    result: dict[str, Any]


class RecoveryEngine:
    def __init__(
        self,
        settings: RecoverySettings,
        session_factory: SessionFactory,
        gateway: KubeGateway,
        audit: AuditService,
        incidents: IncidentService,
        metrics: Metrics,
        clock: Clock = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cfg = settings
        self._factory = session_factory
        self._gw = gateway
        self._audit = audit
        self._incidents = incidents
        self._metrics = metrics
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()

    # ----------------------------------------------------------------------- requesting --
    def request_action(
        self, incident_id: str, action_type: ActionType, requested_by: str
    ) -> tuple[RecoveryAction, bool]:
        """Create a recovery action for an incident. Returns (action, created).

        Idempotent: an equivalent action that is still pending/approved/running is returned
        instead of creating a duplicate.
        """
        denied: list[str] | None = None
        created = False
        with session_scope(self._factory) as s:
            incident = s.get(Incident, incident_id)
            if incident is None:
                raise NotFoundError(f"Incident {incident_id} not found")
            if incident.status == "RESOLVED":
                raise ConflictError("Incident is already resolved; no recovery is needed")

            violations = policies.evaluate_static(
                self._cfg, action_type, ResourceType(incident.resource_type), incident.namespace
            )
            if incident.resource_uid is None:
                violations.append("incident has no resource UID to pin the action to")
            if violations:
                self._audit.record(
                    s,
                    audit_events.RECOVERY_REQUEST_DENIED,
                    requested_by,
                    incident_id=incident.id,
                    resource_type=incident.resource_type,
                    resource_name=incident.resource_name,
                    namespace=incident.namespace,
                    details={"action_type": action_type.value, "violations": violations},
                )
                denied = violations
                action = None
            else:
                action = s.scalars(
                    select(RecoveryAction).where(
                        RecoveryAction.incident_id == incident.id,
                        RecoveryAction.action_type == action_type.value,
                        RecoveryAction.status.in_([x.value for x in ACTIVE_ACTION_STATUSES]),
                    )
                ).first()
                if action is None:
                    action = self._create(s, incident, action_type, requested_by)
                    created = True
        if denied is not None:
            raise PolicyViolationError(denied)
        assert action is not None
        return action, created

    def _create(
        self, s: Session, incident: Incident, action_type: ActionType, requested_by: str
    ) -> RecoveryAction:
        auto_approved = not self._cfg.require_approval
        now = self._clock()
        action = RecoveryAction(
            id=str(uuid.uuid4()),
            incident_id=incident.id,
            action_type=action_type.value,
            resource_type=incident.resource_type,
            resource_name=incident.resource_name,
            resource_uid=incident.resource_uid,
            namespace=incident.namespace,
            requested_by=requested_by,
            approved_by="policy:auto" if auto_approved else None,
            approved_at=now if auto_approved else None,
            status=(
                ActionStatus.APPROVED if auto_approved else ActionStatus.PENDING_APPROVAL
            ).value,
            dry_run=self._cfg.dry_run,
            requested_at=now,
        )
        s.add(action)
        self._audit.record(
            s,
            audit_events.RECOVERY_REQUESTED,
            requested_by,
            incident_id=incident.id,
            action_id=action.id,
            resource_type=incident.resource_type,
            resource_name=incident.resource_name,
            namespace=incident.namespace,
            details={
                "action_type": action_type.value,
                "dry_run": action.dry_run,
                "status": action.status,
            },
        )
        self._metrics.recovery_actions_total.labels(
            action_type=action_type.value, status=action.status
        ).inc()
        return action

    def propose_for_incident(self, incident_id: str) -> None:
        """Called by the monitoring engine for new/reopened incidents (when recovery is enabled)."""
        with session_scope(self._factory) as s:
            incident = s.get(Incident, incident_id)
            suggested = SUGGESTED_ACTIONS.get(incident.problem_type) if incident else None
        if suggested is None:
            return
        try:
            action, created = self.request_action(incident_id, suggested, MONITOR)
        except PolicyViolationError as exc:
            logger.info("no recovery proposed: %s", exc.message, extra=log_context())
            return
        if created and action.status == ActionStatus.APPROVED:
            # require_approval=false: the policy itself approved it; run the whole workflow.
            executed = self.execute(action.id, MONITOR)
            if executed.status == ActionStatus.RUNNING:
                self.start_verification(action.id)

    # ------------------------------------------------------------- approve / reject --
    def approve(self, action_id: str, approver: str) -> RecoveryAction:
        return self._decide(action_id, approver, ActionStatus.APPROVED, None)

    def reject(self, action_id: str, actor: str, reason: str | None = None) -> RecoveryAction:
        return self._decide(action_id, actor, ActionStatus.REJECTED, reason)

    def _decide(
        self, action_id: str, actor: str, target: ActionStatus, reason: str | None
    ) -> RecoveryAction:
        approving = target == ActionStatus.APPROVED
        with session_scope(self._factory) as s:
            action = s.get(RecoveryAction, action_id)
            if action is None:
                raise NotFoundError(f"Recovery action {action_id} not found")
            if approving:
                violations = policies.evaluate_static(
                    self._cfg,
                    ActionType(action.action_type),
                    ResourceType(action.resource_type),
                    action.namespace,
                )
                if violations:
                    raise PolicyViolationError(violations)
            values: dict[str, Any] = {"status": target.value}
            if approving:
                values.update(approved_by=actor, approved_at=self._clock())
            else:
                values.update(error_message=reason)
            claimed = cast(
                "CursorResult[Any]",
                s.execute(
                    update(RecoveryAction)
                    .where(
                        RecoveryAction.id == action_id,
                        RecoveryAction.status == ActionStatus.PENDING_APPROVAL.value,
                    )
                    .values(**values)
                ),
            ).rowcount
            if claimed != 1:
                s.refresh(action)
                verb = "approved" if approving else "rejected"
                raise ConflictError(
                    f"Action is {action.status}; only PENDING_APPROVAL actions can be {verb}"
                )
            s.refresh(action)
            self._audit.record(
                s,
                audit_events.RECOVERY_APPROVED if approving else audit_events.RECOVERY_REJECTED,
                actor,
                incident_id=action.incident_id,
                action_id=action.id,
                resource_type=action.resource_type,
                resource_name=action.resource_name,
                namespace=action.namespace,
                details={"reason": reason} if reason else None,
            )
        self._metrics.recovery_actions_total.labels(
            action_type=action.action_type, status=target.value
        ).inc()
        return action

    # ------------------------------------------------------------------------ execute --
    def execute(self, action_id: str, executor: str) -> RecoveryAction:
        """Claim, validate, then dry-run or perform an APPROVED action.

        Returns the action in its resulting state: DRY_RUN, CANCELLED (policy), FAILED, SUCCEEDED
        (verification disabled) or RUNNING (awaiting ``verify``).
        """
        t = self._claim(action_id)
        try:
            violations, context = self._validate_for_execution(t)
            if violations:
                return self._finalize(
                    t,
                    ActionStatus.CANCELLED,
                    executor,
                    event=audit_events.RECOVERY_BLOCKED,
                    error="policy violation: " + "; ".join(violations),
                    details={"violations": violations},
                    clear_executed=True,
                )
            assert t.uid is not None  # guaranteed by evaluate_*_target
            if t.dry_run:
                return self._finalize(
                    t,
                    ActionStatus.DRY_RUN,
                    executor,
                    event=audit_events.RECOVERY_DRY_RUN,
                    result={
                        "dry_run": True,
                        "summary": action_ops.describe(t.action_type, t.namespace, t.name),
                    },
                    clear_executed=True,
                )
            try:
                outcome = action_ops.perform(
                    self._gw, t.action_type, t.namespace, t.name, t.uid, self._clock()
                )
            except KubernetesError as exc:
                return self._finalize(
                    t,
                    ActionStatus.FAILED,
                    executor,
                    event=audit_events.RECOVERY_FAILED,
                    error=f"Kubernetes rejected the action: {exc.message}",
                )
            result: dict[str, Any] = {"phase": "executed", **outcome, **context}
            if not self._cfg.verify_after_action:
                result["verification"] = "skipped (recovery.verify_after_action=false)"
                return self._finalize(
                    t,
                    ActionStatus.SUCCEEDED,
                    executor,
                    event=audit_events.RECOVERY_EXECUTED,
                    result=result,
                )
            return self._record_executed(t, executor, result)
        except Exception as exc:  # never leave an action stuck in RUNNING
            logger.exception("recovery execution crashed", extra=log_context(action_id=t.action_id))
            return self._finalize(
                t,
                ActionStatus.FAILED,
                executor,
                event=audit_events.RECOVERY_FAILED,
                error=f"internal error during execution: {type(exc).__name__}",
            )

    def _claim(self, action_id: str) -> _Target:
        with session_scope(self._factory) as s:
            action = s.get(RecoveryAction, action_id)
            if action is None:
                raise NotFoundError(f"Recovery action {action_id} not found")
            effective_dry_run = action.dry_run or self._cfg.dry_run  # dry-run can only tighten
            claimed = cast(
                "CursorResult[Any]",
                s.execute(
                    update(RecoveryAction)
                    .where(
                        RecoveryAction.id == action_id,
                        RecoveryAction.status == ActionStatus.APPROVED.value,
                    )
                    .values(
                        status=ActionStatus.RUNNING.value,
                        executed_at=self._clock(),
                        dry_run=effective_dry_run,
                    )
                ),
            ).rowcount
            if claimed != 1:
                s.refresh(action)
                raise ConflictError(
                    f"Action is {action.status}; only an APPROVED action can be executed "
                    "(it may already be running or finished)"
                )
            s.refresh(action)
            if not action.dry_run:
                # Only a real execution means someone/something is actively working the incident.
                incident = s.get(Incident, action.incident_id)
                if incident is not None:
                    self._incidents.mark_investigating(incident)
            return _Target(
                action_id=action.id,
                incident_id=action.incident_id,
                action_type=ActionType(action.action_type),
                resource_type=ResourceType(action.resource_type),
                namespace=action.namespace or "",
                name=action.resource_name,
                uid=action.resource_uid,
                approved_by=action.approved_by,
                dry_run=action.dry_run,
                result=dict(action.result or {}),
            )

    def _validate_for_execution(self, t: _Target) -> tuple[list[str], dict[str, Any]]:
        violations = policies.evaluate_static(
            self._cfg, t.action_type, t.resource_type, t.namespace
        )
        violations += policies.evaluate_approval(self._cfg, t.approved_by)
        context: dict[str, Any] = {}
        try:
            if t.action_type == ActionType.POD_DELETE:
                pod = self._gw.get_pod(t.namespace, t.name)
                violations += policies.evaluate_pod_target(pod, t.uid)
                if pod is not None:
                    context["owner_uid"] = pod.owner_uid
            else:
                dep = self._gw.get_deployment(t.namespace, t.name)
                violations += policies.evaluate_deployment_target(dep, t.uid)
                if dep is not None:
                    context["generation_before"] = dep.generation
        except KubernetesError as exc:
            violations.append(f"cannot verify the target in the cluster: {exc.message}")
        violations += self._limit_violations(t)
        return violations, context

    def _limit_violations(self, t: _Target) -> list[str]:
        now = self._clock()
        real = (
            RecoveryAction.dry_run.is_(False),
            RecoveryAction.id != t.action_id,
            RecoveryAction.status.in_([x.value for x in policies.EXECUTED_STATUSES]),
        )
        with session_scope(self._factory) as s:
            last = s.scalar(
                select(func.max(RecoveryAction.executed_at)).where(
                    RecoveryAction.resource_type == t.resource_type.value,
                    RecoveryAction.namespace == t.namespace,
                    RecoveryAction.resource_name == t.name,
                    RecoveryAction.executed_at.is_not(None),
                    *real,
                )
            )
            in_hour = (
                s.scalar(
                    select(func.count())
                    .select_from(RecoveryAction)
                    .where(
                        RecoveryAction.executed_at >= now - _HOUR,
                        *real,
                    )
                )
                or 0
            )
            recent = s.scalars(
                select(RecoveryAction.status)
                .where(
                    RecoveryAction.resource_type == t.resource_type.value,
                    RecoveryAction.namespace == t.namespace,
                    RecoveryAction.resource_name == t.name,
                    RecoveryAction.dry_run.is_(False),
                    RecoveryAction.id != t.action_id,
                    RecoveryAction.status.in_(
                        [ActionStatus.SUCCEEDED.value, ActionStatus.FAILED.value]
                    ),
                )
                .order_by(RecoveryAction.executed_at.desc())
                .limit(self._cfg.max_consecutive_failures)
            ).all()
        consecutive_failures = 0
        for status in recent:
            if status != ActionStatus.FAILED.value:
                break
            consecutive_failures += 1
        return policies.evaluate_limits(self._cfg, now, last, in_hour, consecutive_failures)

    def _record_executed(self, t: _Target, executor: str, result: dict[str, Any]) -> RecoveryAction:
        """Cluster call succeeded; the action stays RUNNING until verification gives a verdict."""
        with session_scope(self._factory) as s:
            action = s.get(RecoveryAction, t.action_id)
            assert action is not None
            action.result = {**(action.result or {}), **result}
            self._audit.record(
                s,
                audit_events.RECOVERY_EXECUTED,
                executor,
                incident_id=t.incident_id,
                action_id=t.action_id,
                resource_type=t.resource_type.value,
                resource_name=t.name,
                namespace=t.namespace,
                details={"operation": result.get("operation")},
            )
        return action

    # ------------------------------------------------------- background verification --
    def start_verification(self, action_id: str) -> None:
        """Verify a RUNNING action in a tracked background thread (joined on shutdown)."""
        thread = threading.Thread(
            target=self._verify_guarded,
            args=(action_id,),
            name=f"verify-{action_id[:8]}",
            daemon=True,
        )
        with self._threads_lock:
            self._threads.add(thread)
        thread.start()

    def _verify_guarded(self, action_id: str) -> None:
        try:
            self.verify(action_id)
        except Exception:  # an unverified action is failed by fail_interrupted() on next start
            logger.exception(
                "background verification crashed", extra=log_context(action_id=action_id)
            )
        finally:
            with self._threads_lock:
                self._threads.discard(threading.current_thread())

    def shutdown(self, timeout: float = 30.0) -> int:
        """Wait for in-flight verifications. Returns how many were still running at the timeout."""
        deadline = time.monotonic() + timeout
        with self._threads_lock:
            pending = list(self._threads)
        for thread in pending:
            thread.join(max(deadline - time.monotonic(), 0.0))
        return sum(1 for t in pending if t.is_alive())

    # ------------------------------------------------------------------------ verify --
    def verify(self, action_id: str) -> RecoveryAction:
        """Judge the resulting cluster state and finalise a RUNNING action. Idempotent."""
        with session_scope(self._factory) as s:
            action = s.get(RecoveryAction, action_id)
            if action is None:
                raise NotFoundError(f"Recovery action {action_id} not found")
            if (
                action.status != ActionStatus.RUNNING
                or (action.result or {}).get("phase") != "executed"
            ):
                return action
            t = _Target(
                action_id=action.id,
                incident_id=action.incident_id,
                action_type=ActionType(action.action_type),
                resource_type=ResourceType(action.resource_type),
                namespace=action.namespace or "",
                name=action.resource_name,
                uid=action.resource_uid,
                approved_by=action.approved_by,
                dry_run=action.dry_run,
                result=dict(action.result or {}),
            )
        assert t.uid is not None
        uid = t.uid
        outcome = verification.wait_for_outcome(
            lambda: verification.check_once(
                self._gw, t.action_type, t.namespace, t.name, uid, t.result
            ),
            self._cfg.verification_timeout_seconds,
            self._cfg.verification_poll_seconds,
            monotonic=self._monotonic,
            sleep=self._sleep,
        )
        if outcome.verdict == "success":
            return self._finalize(
                t,
                ActionStatus.SUCCEEDED,
                SYSTEM,
                event=audit_events.RECOVERY_VERIFIED,
                result={"verification": outcome.detail},
                verified=True,
                resolve_incident=True,
            )
        return self._finalize(
            t,
            ActionStatus.FAILED,
            SYSTEM,
            event=audit_events.RECOVERY_FAILED,
            error=outcome.detail,
            result={"verification": outcome.detail},
        )

    def execute_and_verify(self, action_id: str, executor: str) -> RecoveryAction:
        action = self.execute(action_id, executor)
        if action.status == ActionStatus.RUNNING:
            return self.verify(action_id)
        return action

    def fail_interrupted(self) -> int:
        """Startup hygiene: a RUNNING action whose process died can never be verified."""
        with session_scope(self._factory) as s:
            ids = list(
                s.scalars(
                    select(RecoveryAction.id).where(
                        RecoveryAction.status == ActionStatus.RUNNING.value
                    )
                )
            )
        for action_id in ids:
            with session_scope(self._factory) as s:
                a = s.get(RecoveryAction, action_id)
                assert a is not None
                t = _Target(
                    a.id,
                    a.incident_id,
                    ActionType(a.action_type),
                    ResourceType(a.resource_type),
                    a.namespace or "",
                    a.resource_name,
                    a.resource_uid,
                    a.approved_by,
                    a.dry_run,
                    {},
                )
            self._finalize(
                t,
                ActionStatus.FAILED,
                SYSTEM,
                event=audit_events.RECOVERY_FAILED,
                error="interrupted: KubiDoor stopped before this action could be verified; "
                "its outcome is unknown",
            )
        return len(ids)

    # ---------------------------------------------------------------------- finalising --
    def _finalize(
        self,
        t: _Target,
        status: ActionStatus,
        actor: str,
        *,
        event: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        verified: bool = False,
        clear_executed: bool = False,
        resolve_incident: bool = False,
        details: dict[str, Any] | None = None,
    ) -> RecoveryAction:
        now = self._clock()
        with session_scope(self._factory) as s:
            action = s.get(RecoveryAction, t.action_id)
            assert action is not None
            if action.status != ActionStatus.RUNNING:
                return action  # already finalised by someone else: stay idempotent
            started = action.executed_at
            action.status = status.value
            if result:
                action.result = {**(action.result or {}), **result}
            if error:
                action.error_message = redact(error)[:2000]
            if verified:
                action.verified_at = now
            if clear_executed:
                action.executed_at = None
            self._audit.record(
                s,
                event,
                actor,
                incident_id=t.incident_id,
                action_id=t.action_id,
                resource_type=t.resource_type.value,
                resource_name=t.name,
                namespace=t.namespace,
                details={
                    "status": status.value,
                    **(details or {}),
                    **({"error": action.error_message} if error else {}),
                },
            )
            if resolve_incident:
                incident = s.get(Incident, t.incident_id)
                if incident is not None and incident.status != "RESOLVED":
                    self._incidents.resolve_in_session(
                        s,
                        incident,
                        actor="recovery",
                        note=f"Recovery action {t.action_id} verified: "
                        f"{(result or {}).get('verification', '')}",
                        event_type=audit_events.INCIDENT_AUTO_RESOLVED,
                        now=now,
                    )
        label = {"action_type": t.action_type.value}
        self._metrics.recovery_actions_total.labels(**label, status=status.value).inc()
        if status == ActionStatus.FAILED:
            self._metrics.recovery_failures_total.labels(**label).inc()
        if status in (ActionStatus.SUCCEEDED, ActionStatus.FAILED) and started is not None:
            self._metrics.recovery_duration_seconds.labels(**label).observe(
                max((now - started).total_seconds(), 0.0)
            )
        logger.info(
            "recovery action %s -> %s",
            t.action_id,
            status.value,
            extra=log_context(
                action_id=t.action_id,
                incident_id=t.incident_id,
                resource_type=t.resource_type.value,
                resource_name=t.name,
                namespace=t.namespace,
            ),
        )
        return action

    # ------------------------------------------------------------------------ queries --
    def get_action(self, action_id: str) -> RecoveryAction:
        with session_scope(self._factory) as s:
            action = s.get(RecoveryAction, action_id)
            if action is None:
                raise NotFoundError(f"Recovery action {action_id} not found")
            return action

    def list_actions(
        self,
        *,
        status: ActionStatus | None,
        incident_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[RecoveryAction], int]:
        where: list[Any] = []
        if status:
            where.append(RecoveryAction.status == status.value)
        if incident_id:
            where.append(RecoveryAction.incident_id == incident_id)
        with session_scope(self._factory) as s:
            total = s.scalar(select(func.count()).select_from(RecoveryAction).where(*where)) or 0
            rows = s.scalars(
                select(RecoveryAction)
                .where(*where)
                .order_by(RecoveryAction.requested_at.desc(), RecoveryAction.id)
                .limit(limit)
                .offset(offset)
            ).all()
        return list(rows), total
