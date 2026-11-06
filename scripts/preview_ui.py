"""Preview the KubiDoor dashboard with a FAKE in-memory cluster (no Kubernetes needed).

    python scripts/preview_ui.py            # then open http://127.0.0.1:8765/

Everything shown is made-up demo data and nothing real is touched. Recovery is enabled here
(dry run + approval) only so you can try the approval flow against the fake cluster.
"""

import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402
from tests import fakes  # noqa: E402

from kubidoor.container import build_services  # noqa: E402
from kubidoor.core.config import RecoverySettings, Settings  # noqa: E402
from kubidoor.database.session import run_migrations  # noqa: E402
from kubidoor.kubernetes.models import ResourceUsage  # noqa: E402
from kubidoor.main import create_app  # noqa: E402

NOW = datetime.now(UTC)
fakes.T0 = NOW  # the builders express ages relative to this


def demo_cluster() -> fakes.FakeGateway:
    gw = fakes.FakeGateway()
    gw.version = "v1.31.2"
    gw.nodes = [fakes.node("kind-control-plane"), fakes.node("kind-worker"), fakes.node("kind-worker2")]
    gw.node_metrics = {
        "kind-control-plane": ResourceUsage(310, 1_100_000_000),
        "kind-worker": ResourceUsage(520, 1_900_000_000),
        "kind-worker2": ResourceUsage(415, 1_500_000_000),
    }
    healthy = [fakes.pod(f"web-6d8f7-{i}", node=f"kind-worker{'' if i % 2 else '2'}") for i in range(3)]
    healthy += [fakes.pod(f"api-5c9b4-{i}", containers=[fakes.container("api", image="ghcr.io/acme/api:2.3")]) for i in range(2)]
    gw.pods = healthy + [
        fakes.crashloop_pod("payments-7b6c9-x2k4q", restarts=9),
        fakes.pod("search-84d5f-m8p2z", containers=[fakes.container(
            "search", state="waiting", reason="ImagePullBackOff", ready=False,
            image="registry.example.com/search:9.9.9", message="Back-off pulling image")]),
        fakes.pod("report-job-0", containers=[fakes.container(
            "worker", ready=False, restarts=3, last_reason="OOMKilled", last_exit_code=137,
            last_finished_at=NOW - timedelta(minutes=2), limits={"memory": "128Mi"})]),
        fakes.pod("batch-1", phase="Succeeded", ready=False,
                  containers=[fakes.container("job", state="terminated", reason="Completed", ready=False)]),
    ]
    gw.deployments = [
        fakes.deployment("web", replicas=3), fakes.deployment("api", replicas=2),
        fakes.deployment("payments", replicas=2, ready=0, available=0, age=timedelta(hours=2)),
        fakes.deployment("search", replicas=2, ready=1, available=1, age=timedelta(hours=2)),
    ]
    return gw


def main() -> None:
    db = Path(tempfile.mkdtemp(prefix="kubidoor-preview-")) / "preview.db"
    settings = Settings(
        database_url=f"sqlite:///{db}",
        auto_migrate=False,
        migrations_dir=str(ROOT / "migrations"),
        monitoring={"enabled": True, "interval_seconds": 10},  # type: ignore[arg-type]
        recovery=RecoverySettings(enabled=True, dry_run=True, require_approval=True),
    )
    run_migrations(settings.database_url, settings.migrations_dir)
    gw = demo_cluster()
    services = build_services(settings, gateway=gw)
    services.monitoring.run_cycle()  # incidents exist the moment the page opens
    port = int(os.environ.get("PORT", "8765"))
    print(f"KubiDoor preview with FAKE data: http://127.0.0.1:{port}/")
    uvicorn.run(create_app(settings, services), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
