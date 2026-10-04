"""Sign-in: sessions, the PayPal login flow, and the dev login."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_paypal
from app.main import app
from app.models import AuthSession, User
from tests.fakes import FakePayPal, sign_in

SETTINGS = Settings(public_api_url="http://api.test", frontend_url="http://app.test", dev_login=True)


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def make_client(session_factory, paypal):
    def _session():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_paypal] = lambda: paypal
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    yield lambda: TestClient(app, follow_redirects=False)  # each call is a separate browser
    app.dependency_overrides.clear()


# ---- dev login -------------------------------------------------------------

def test_dev_login_is_invisible_unless_enabled(make_client):
    app.dependency_overrides[get_settings] = lambda: Settings(dev_login=False)
    r = make_client().post("/auth/dev-login", json={"name": "A", "email": "a@b.co"})
    assert r.status_code == 404
    assert make_client().get("/auth/me").json()["dev_login"] is False


def test_dev_login_signs_you_in(make_client):
    c = make_client()
    assert c.get("/auth/me").json()["user"] is None
    sign_in(c, name="Ann", email="ann@example.com")
    me = c.get("/auth/me").json()
    assert me["user"]["name"] == "Ann" and me["user"]["paypal_verified"] is True and me["shop"] is None


def test_the_cookie_is_httponly_and_only_a_hash_is_stored(make_client, session_factory):
    c = make_client()
    r = c.post("/auth/dev-login", json={"name": "Ann", "email": "ann@example.com"})
    header = r.headers["set-cookie"]
    assert "HttpOnly" in header and "SameSite=lax" in header
    token = c.cookies.get(auth.SESSION_COOKIE)
    with session_factory() as s:
        stored = s.scalars(select(AuthSession.token_hash)).all()
    assert stored == [auth.hash_token(token)] and token not in stored


def test_logout_ends_the_session(make_client, session_factory):
    c = make_client()
    sign_in(c)
    assert c.post("/auth/logout").json() == {"ok": True}
    assert c.get("/auth/me").json()["user"] is None
    with session_factory() as s:
        assert s.scalars(select(AuthSession)).all() == []


def test_an_expired_session_does_not_work(make_client, session_factory):
    c = make_client()
    sign_in(c)
    with session_factory() as s:
        row = s.scalars(select(AuthSession)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    assert c.get("/auth/me").json()["user"] is None


def test_a_made_up_cookie_is_rejected(make_client):
    c = make_client()
    c.cookies.set(auth.SESSION_COOKIE, "not-a-real-token")
    assert c.get("/auth/me").json()["user"] is None


def test_each_dev_login_with_the_same_email_is_the_same_user(make_client, session_factory):
    sign_in(make_client(), email="same@example.com", name="First")
    sign_in(make_client(), email="same@example.com", name="Second")
    with session_factory() as s:
        users = s.scalars(select(User)).all()
    assert len(users) == 1 and users[0].name == "Second"


# ---- PayPal login -----------------------------------------------------------

def test_login_sends_you_to_paypal_with_a_state_cookie(make_client):
    c = make_client()
    r = c.get("/auth/paypal/login", params={"next": "/sell"})
    assert r.status_code == 303
    target = urlparse(r.headers["location"])
    assert target.netloc == "fake.paypal"
    q = parse_qs(target.query)
    assert q["redirect_uri"] == ["http://api.test/auth/paypal/callback"]
    assert c.cookies.get(auth.STATE_COOKIE) == q["state"][0]
    assert c.cookies.get(auth.NEXT_COOKIE).strip('"') == "/sell"  # quoted on the wire because of the "/"


def start_login(c, next_path="/buy"):
    r = c.get("/auth/paypal/login", params={"next": next_path})
    return parse_qs(urlparse(r.headers["location"]).query)["state"][0]


def test_successful_login_creates_a_user_and_a_session(make_client, session_factory):
    c = make_client()
    state = start_login(c, "/sell")
    r = c.get("/auth/paypal/callback", params={"code": "abc", "state": state})
    assert r.status_code == 303 and r.headers["location"] == "http://app.test/sell"
    me = c.get("/auth/me").json()
    assert me["user"]["email"] == "sam@example.com" and me["user"]["paypal_verified"] is True
    with session_factory() as s:
        assert s.scalars(select(User)).one().paypal_payer_id == "PAYER-1"


def test_signing_in_again_updates_the_same_user_and_notices_verification(make_client, session_factory, paypal):
    paypal.login_profile["verified_account"] = False
    c = make_client()
    c.get("/auth/paypal/callback", params={"code": "x", "state": start_login(c)})
    assert c.get("/auth/me").json()["user"]["paypal_verified"] is False

    paypal.login_profile["verified_account"] = True  # they verified their PayPal account since
    c2 = make_client()
    c2.get("/auth/paypal/callback", params={"code": "y", "state": start_login(c2)})
    assert c2.get("/auth/me").json()["user"]["paypal_verified"] is True
    with session_factory() as s:
        assert len(s.scalars(select(User)).all()) == 1


def test_a_forged_callback_is_refused(make_client):
    c = make_client()
    start_login(c)
    r = c.get("/auth/paypal/callback", params={"code": "abc", "state": "attacker-chosen"})
    assert r.headers["location"] == "http://app.test/signin?error=state"
    assert c.get("/auth/me").json()["user"] is None


def test_a_callback_with_no_state_cookie_is_refused(make_client):
    r = make_client().get("/auth/paypal/callback", params={"code": "abc", "state": "whatever"})
    assert r.headers["location"].endswith("/signin?error=state")


def test_cancelling_on_paypal_goes_back_to_signin(make_client):
    c = make_client()
    start_login(c)
    r = c.get("/auth/paypal/callback", params={"error": "access_denied", "state": "x"})
    assert r.headers["location"].endswith("/signin?error=cancelled")


def test_a_paypal_failure_is_reported_not_raised(make_client, paypal):
    paypal.login_fails = True
    c = make_client()
    r = c.get("/auth/paypal/callback", params={"code": "abc", "state": start_login(c)})
    assert r.headers["location"].endswith("/signin?error=paypal")
    assert c.get("/auth/me").json()["user"] is None


@pytest.mark.parametrize("evil", ["//evil.com", "https://evil.com/x", "/\\evil.com", "evil.com", ""])
def test_next_can_never_leave_our_site(evil):
    assert auth.safe_next(evil) == "/"


def test_safe_paths_are_kept():
    assert auth.safe_next("/sell") == "/sell" and auth.safe_next("/orders/5?status=x") == "/orders/5?status=x"


def test_the_state_cookie_is_single_use(make_client):
    c = make_client()
    state = start_login(c)
    c.get("/auth/paypal/callback", params={"code": "abc", "state": state})
    again = c.get("/auth/paypal/callback", params={"code": "abc", "state": state})
    assert again.headers["location"].endswith("/signin?error=state")
