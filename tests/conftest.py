"""Shared fixtures: in-memory SQLite, fake cluster, fake clock, fully wired services."""

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kubidoor.container import Services, build_services
from kubidoor.core.config import RecoverySettings, Settings
from kubidoor.database.base import Base
from kubidoor.main import create_app
from tests.fakes import FakeClock, FakeGateway

ServiceFactory = Callable[..., Services]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


def make_settings(database_url: str = "sqlite://", **recovery: Any) -> Settings:
    rec = {
        "enabled": True,
        "dry_run": False,
        "require_approval": True,
        "allowed_namespaces": ["demo"],
        "verification_timeout_seconds": 5,
        "verification_poll_seconds": 0.01,
        "cooldown_seconds": 300,
        **recovery,
    }
    return Settings(
        database_url=database_url,
        auto_migrate=False,
        monitoring={"enabled": False, "interval_seconds": 30},  # type: ignore[arg-type]
        recovery=RecoverySettings(**rec),
    )


@pytest.fixture
def make_services(gateway: FakeGateway, clock: FakeClock) -> Iterator[ServiceFactory]:
    created: list[Services] = []

    def factory(database_url: str = "sqlite://", **recovery: Any) -> Services:
        svc = build_services(make_settings(database_url, **recovery), gateway=gateway, clock=clock)
        Base.metadata.create_all(svc.db_engine)
        # Verification polling must not really sleep or time out in tests.
        svc.recovery._sleep = lambda _s: clock.advance(seconds=1)
        svc.recovery._monotonic = lambda: clock.now.timestamp()
        created.append(svc)
        return svc

    yield factory
    for svc in created:
        svc.recovery.shutdown(timeout=10)
        svc.db_engine.dispose()


@pytest.fixture
def services(make_services: ServiceFactory) -> Services:
    return make_services()


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    app = create_app(services.settings, services)
    with TestClient(app) as c:
        yield c
