"""Schema, migrations, constraints, audit immutability and session safety."""

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from kubidoor.audit.service import AuditImmutableError, AuditService
from kubidoor.database.base import Base
from kubidoor.database.models import AuditLog, Incident, MonitoringHistory, RecoveryAction
from kubidoor.database.session import (
    check_database,
    create_db_engine,
    create_session_factory,
    render_url,
    run_migrations,
    session_scope,
)
from tests.fakes import T0

pytestmark = pytest.mark.integration


def new_incident(fp: str = "f" * 64, **kw) -> Incident:
    return Incident(
        id=str(uuid.uuid4()),
        fingerprint=fp,
        resource_type="Pod",
        resource_name="p",
        namespace="demo",
        problem_type="CrashLoopBackOff",
        severity="HIGH",
        status="OPEN",
        title="t",
        description="",
        probable_cause="",
        recommended_action="",
        evidence={},
        first_detected_at=T0,
        last_detected_at=T0,
        occurrence_count=1,
        reopen_count=0,
        **kw,
    )


@pytest.fixture
def factory(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_db_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    yield create_session_factory(engine)
    engine.dispose()


def test_migrations_create_exactly_the_required_tables_and_indexes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path / 'mig.db'}"
    run_migrations(url, "migrations")
    engine = create_db_engine(url)
    insp = inspect(engine)
    assert {"incidents", "recovery_actions", "monitoring_history", "audit_logs"} <= set(
        insp.get_table_names()
    )
    assert insp.get_foreign_keys("recovery_actions")[0]["referred_table"] == "incidents"
    assert any(
        u["column_names"] == ["fingerprint"] for u in insp.get_unique_constraints("incidents")
    )
    assert any(
        set(i["column_names"]) == {"status", "severity"} for i in insp.get_indexes("incidents")
    )
    assert insp.get_indexes("audit_logs") and insp.get_indexes("recovery_actions")
    engine.dispose()


def test_migrations_match_orm_models_exactly(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """No drift: autogenerate against a migrated DB finds nothing to change."""
    url = f"sqlite:///{tmp_path / 'drift.db'}"
    run_migrations(url, "migrations")
    run_migrations(url, "migrations")  # idempotent
    engine = create_db_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_missing_migrations_dir_is_a_clear_configuration_error() -> None:
    from kubidoor.core.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        run_migrations("sqlite://", "/nonexistent/migrations")


def test_fingerprint_is_unique(factory) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        s.add(new_incident("a" * 64))
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(new_incident("a" * 64))


def test_foreign_keys_are_enforced(factory) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(IntegrityError), session_scope(factory) as s:
        s.add(
            RecoveryAction(
                id=str(uuid.uuid4()),
                incident_id="no-such-incident",
                action_type="pod_delete",
                resource_type="Pod",
                resource_name="p",
                requested_by="x",
                status="PENDING_APPROVAL",
                dry_run=True,
                requested_at=T0,
            )
        )


def test_failed_transaction_rolls_back_everything(factory) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RuntimeError), session_scope(factory) as s:
        s.add(new_incident("b" * 64))
        s.flush()
        raise RuntimeError("boom")
    with session_scope(factory) as s:
        assert s.scalar(text("select count(*) from incidents")) == 0


def test_timestamps_are_utc_roundtrip_and_naive_is_rejected(factory) -> None:  # type: ignore[no-untyped-def]
    tbilisi = timezone(timedelta(hours=4))
    local = datetime(2026, 11, 3, 16, 0, tzinfo=tbilisi)  # == 12:00 UTC
    inc = new_incident("c" * 64)
    inc.first_detected_at = local
    with session_scope(factory) as s:
        s.add(inc)
    with session_scope(factory) as s:
        got = s.get(Incident, inc.id)
        assert got.first_detected_at == datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
        assert got.first_detected_at.utcoffset() == timedelta(0)
    bad = new_incident("d" * 64)
    bad.last_detected_at = datetime(2026, 11, 3, 12, 0)  # naive
    with pytest.raises(Exception, match="naive datetime"), session_scope(factory) as s:
        s.add(bad)


def test_audit_log_cannot_be_updated_or_deleted_through_the_orm(factory) -> None:  # type: ignore[no-untyped-def]
    audit = AuditService(lambda: T0)
    with session_scope(factory) as s:
        entry = audit.record(s, "INCIDENT_RESOLVED", "alice", incident_id="i1", details={"a": 1})
        s.flush()
        entry_id = entry.id
    with pytest.raises(AuditImmutableError), session_scope(factory) as s:
        row = s.get(AuditLog, entry_id)
        row.actor = "mallory"
    with pytest.raises(AuditImmutableError), session_scope(factory) as s:
        s.delete(s.get(AuditLog, entry_id))
    with session_scope(factory) as s:
        row = s.get(AuditLog, entry_id)
        assert row.actor == "alice" and row.details == {"a": 1}


def test_audit_service_exposes_no_mutation_api() -> None:
    public = {n for n in dir(AuditService) if not n.startswith("_")}
    assert public == {"record", "for_incident"}


def test_audit_record_rolls_back_with_its_transaction(factory) -> None:  # type: ignore[no-untyped-def]
    audit = AuditService(lambda: T0)
    with pytest.raises(RuntimeError), session_scope(factory) as s:
        audit.record(s, "X", "a")
        raise RuntimeError
    with session_scope(factory) as s:
        assert s.scalar(text("select count(*) from audit_logs")) == 0


def test_monitoring_history_table_works(factory) -> None:  # type: ignore[no-untyped-def]
    with session_scope(factory) as s:
        s.add(MonitoringHistory(started_at=T0, finished_at=T0, duration_seconds=0.2, status="ok"))
    with session_scope(factory) as s:
        assert s.scalar(text("select count(*) from monitoring_history")) == 1


def test_db_health_check_and_password_masking() -> None:
    engine = create_db_engine("sqlite://")
    assert check_database(engine) is True
    engine.dispose()
    assert check_database(engine) is True  # sqlite recreates; contrast: bad file path below
    bad = create_db_engine("sqlite:////nonexistent-dir/x.db")
    assert check_database(bad) is False
    masked = render_url("postgresql://kubidoor:hunter2@db:5432/kubidoor")
    assert "hunter2" not in masked and "kubidoor:***@db" in masked


def test_unsupported_database_url_fails_startup_validation() -> None:
    from pydantic import ValidationError

    from kubidoor.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(database_url="mysql://x")


def test_settings_env_overrides_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    from kubidoor.core.config import Settings

    monkeypatch.setenv("KUBIDOOR_RECOVERY__ENABLED", "true")
    monkeypatch.setenv("KUBIDOOR_RECOVERY__ALLOWED_NAMESPACES", '["kubidoor-demo"]')
    monkeypatch.setenv("KUBIDOOR_MONITORING__INTERVAL_SECONDS", "15")
    monkeypatch.setenv("KUBIDOOR_API_TOKEN", "zq9-distinctive-secret")
    s = Settings()
    assert s.recovery.enabled is True and s.recovery.allowed_namespaces == ["kubidoor-demo"]
    assert (
        s.recovery.dry_run is True and s.recovery.require_approval is True
    )  # untouched defaults stay safe
    assert s.monitoring.interval_seconds == 15 and "zq9-distinctive-secret" not in repr(s)
    monkeypatch.setenv("KUBIDOOR_MONITORING__INTERVAL_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_blank_api_token_disables_auth_instead_of_requiring_an_empty_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kubidoor.core.config import Settings

    for blank in ("", "   "):
        monkeypatch.setenv("KUBIDOOR_API_TOKEN", blank)
        assert Settings().api_token is None
    monkeypatch.setenv("KUBIDOOR_API_TOKEN", "real-token")
    assert Settings().api_token is not None
