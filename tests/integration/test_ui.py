"""The web dashboard: served as static files, locked down by CSP, never loads anything external."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from kubidoor.container import Services
from kubidoor.main import UI_DIR, create_app

pytestmark = pytest.mark.integration


def test_dashboard_is_served_with_strict_security_headers(client: TestClient) -> None:
    res = client.get("/ui/")
    assert res.status_code == 200 and "<title>KubiDoor</title>" in res.text
    csp = res.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    assert res.headers["x-content-type-options"] == "nosniff"
    for asset in ("app.js", "app.css"):
        r = client.get(f"/ui/{asset}")
        assert r.status_code == 200 and "content-security-policy" in r.headers


def test_root_redirects_to_dashboard(client: TestClient) -> None:
    res = client.get("/", follow_redirects=False)
    assert res.status_code in (302, 307) and res.headers["location"] == "/ui/"


def test_api_responses_do_not_get_the_ui_csp(client: TestClient) -> None:
    assert "content-security-policy" not in client.get("/health").headers


def test_dashboard_files_are_public_but_the_data_still_needs_the_token(services: Services) -> None:
    services.settings.api_token = SecretStr("s3cret")
    with TestClient(create_app(services.settings, services)) as c:
        assert c.get("/ui/").status_code == 200  # the page itself holds no data
        assert c.get("/api/v1/incidents").status_code == 401
        assert c.get("/api/v1/recovery/policy").status_code == 401


def test_policy_endpoint_reports_the_active_safety_policy(client: TestClient) -> None:
    body = client.get("/api/v1/recovery/policy").json()
    assert body["enabled"] is True and body["require_approval"] is True
    assert body["allowed_actions"] == ["deployment_restart", "pod_delete"]
    assert body["blocked_namespaces"] == ["kube-system"]


def test_policy_defaults_stay_safe_when_nothing_is_configured() -> None:
    from kubidoor.core.config import Settings

    p = Settings().recovery
    assert (p.enabled, p.dry_run, p.require_approval) == (False, True, True)


def _sources() -> dict[str, str]:
    return {f.name: f.read_text() for f in UI_DIR.iterdir() if f.is_file()}


def test_dashboard_never_loads_anything_from_another_origin() -> None:
    for name, text in _sources().items():
        urls = re.findall(r"https?://[^\s\"')]+", text)
        allowed = {"http://www.w3.org/2000/svg"}
        assert set(urls) <= allowed, f"{name} references external URLs: {urls}"


def test_dashboard_renders_cluster_data_as_text_only() -> None:
    js = re.sub(r"/\*.*?\*/", "", _sources()["app.js"], flags=re.S)  # ignore comments
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert banned not in js, f"app.js must not use {banned}"
    assert 'setAttribute("style"' not in js  # inline style attributes are blocked by the CSP


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_javascript_parses() -> None:
    result = subprocess.run(  # noqa: S603
        [shutil.which("node") or "node", "--check", str(UI_DIR / "app.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_static_directory_is_packaged() -> None:
    assert Path(UI_DIR, "index.html").is_file()
    assert (
        'kubidoor = ["ui/static/*"]'
        in Path(__file__).parents[2].joinpath("pyproject.toml").read_text()
    )
