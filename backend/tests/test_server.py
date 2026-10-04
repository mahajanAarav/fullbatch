"""The deployed shape: API under /api, React app everywhere else."""

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import server


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>THE APP</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log('hi')")
    return tmp_path


def test_api_lives_under_slash_api(dist):
    c = TestClient(server.build_site(dist))
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/health").text.startswith("<html>")  # not an API path: the app answers


def test_real_files_are_served_as_themselves(dist):
    c = TestClient(server.build_site(dist))
    assert c.get("/assets/app.js").text == "console.log('hi')"
    assert c.get("/").text == "<html>THE APP</html>"


@pytest.mark.parametrize("page", ["/buy", "/sell", "/orders/7", "/signin"])
def test_react_pages_get_index_html_so_reload_and_deep_links_work(dist, page):
    assert TestClient(server.build_site(dist)).get(page).text == "<html>THE APP</html>"


def test_a_missing_file_is_a_real_404_not_the_app(dist):
    c = TestClient(server.build_site(dist))
    assert c.get("/assets/missing.js").status_code == 404
    assert c.get("/favicon.png").status_code == 404


def test_unknown_api_paths_are_a_json_404_never_the_app(dist):
    r = TestClient(server.build_site(dist)).get("/api/does-not-exist")
    assert r.status_code == 404 and "<html>" not in r.text


def test_without_a_built_frontend_the_api_still_works(tmp_path):
    c = TestClient(server.build_site(tmp_path / "missing"))
    assert c.get("/api/health").status_code == 200
    assert "not been built" in c.get("/").json()["message"]


def test_sign_in_works_through_the_mount_and_the_cookie_applies_site_wide(dist, session_factory):
    from app.config import Settings, get_settings
    from app.db import get_session
    from app.main import app as api

    def _session():
        with session_factory() as s:
            yield s

    api.dependency_overrides[get_session] = _session
    api.dependency_overrides[get_settings] = lambda: Settings(dev_login=True)
    try:
        c = TestClient(server.build_site(dist))
        assert c.get("/api/auth/me").json()["user"] is None
        assert c.post("/api/auth/dev-login", json={"name": "Ann", "email": "ann@example.com"}).status_code == 200
        assert c.get("/api/auth/me").json()["user"]["name"] == "Ann"
    finally:
        api.dependency_overrides.clear()


def test_the_deadline_loop_runs_and_stops(monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr("app.db.get_session_factory", lambda: "factory")
    monkeypatch.setattr("app.deps.get_paypal", lambda: "paypal")
    monkeypatch.setattr("app.drops.run_deadline_job", lambda f, p: (ran.set(), {"settled": [], "expired": 0, "errors": []})[1])
    stop = server.start_deadline_loop(0.01)
    assert ran.wait(2), "the job never ran"
    stop.set()


def test_a_crashing_job_does_not_kill_the_loop(monkeypatch):
    calls = []

    def boom(f, p):
        calls.append(1)
        raise RuntimeError("database down")

    monkeypatch.setattr("app.db.get_session_factory", lambda: "factory")
    monkeypatch.setattr("app.deps.get_paypal", lambda: "paypal")
    monkeypatch.setattr("app.drops.run_deadline_job", boom)
    stop = server.start_deadline_loop(0.01)
    deadline = threading.Event()
    deadline.wait(0.3)
    stop.set()
    assert len(calls) >= 2  # it kept going after the first crash
