"""Email verification: one-time codes, and the rules that depend on a verified email."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth, mailer
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_mailer, get_paypal
from app.main import app
from app.models import EmailCode, User
from app.ratelimit import RateLimiter
from tests.fakes import FakeMailer, FakePayPal, ScriptedLLM, sign_in

SETTINGS = Settings(public_api_url="http://api.test", frontend_url="http://app.test", dev_login=True)


@pytest.fixture
def env(session_factory):
    box = {"mailer": FakeMailer(), "settings": SETTINGS}

    def _session():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_paypal] = lambda: FakePayPal()
    app.dependency_overrides[get_mailer] = lambda: box["mailer"]
    app.dependency_overrides[get_settings] = lambda: box["settings"]
    auth.send_limiter = RateLimiter(limit=5, window=3600)
    box["factory"] = session_factory
    yield box
    app.dependency_overrides.clear()


def unverified(env, email="ann@example.com"):
    c = TestClient(app)
    sign_in(c, "Ann", email, email_verified=False)
    return c


def skip_cooldown(env, user_email="ann@example.com"):
    """Pretend the last code was sent a while ago, so a new one may be requested."""
    with env["factory"]() as s:
        for row in s.scalars(select(EmailCode)):
            row.created_at -= timedelta(seconds=auth.RESEND_SECONDS + 1)
        s.commit()


# ---- the happy path -----------------------------------------------------------

def test_a_code_is_emailed_and_verifying_it_confirms_the_email(env):
    c = unverified(env)
    assert c.get("/auth/me").json()["user"]["email_verified"] is False
    sent = c.post("/auth/email/send").json()
    assert sent == {"sent": True, "email": "a**@example.com"}          # the address is masked in the reply
    mail = env["mailer"].sent[0]
    assert mail["to"] == "ann@example.com" and "verification code" in mail["subject"].lower()

    me = c.post("/auth/email/verify", json={"code": env["mailer"].last_code()}).json()
    assert me["user"]["email_verified"] is True
    assert c.get("/auth/me").json()["user"]["email_verified"] is True


def test_only_a_hash_of_the_code_is_stored(env):
    c = unverified(env)
    c.post("/auth/email/send")
    code = env["mailer"].last_code()
    with env["factory"]() as s:
        row = s.scalars(select(EmailCode)).one()
    assert code not in row.code_hash and len(row.code_hash) == 64


def test_already_verified_people_are_not_emailed(env):
    c = TestClient(app)
    sign_in(c)   # dev sign-in defaults to a verified email
    assert c.post("/auth/email/send").json() == {"already_verified": True}
    assert env["mailer"].sent == []


# ---- wrong, old and reused codes ------------------------------------------------

def test_wrong_codes_count_down_then_lock(env):
    c = unverified(env)
    c.post("/auth/email/send")
    right = env["mailer"].last_code()
    wrong = "000000" if right != "000000" else "111111"
    messages = [c.post("/auth/email/verify", json={"code": wrong}).json()["detail"] for _ in range(5)]
    assert "4 tries left" in messages[0] and "1 try left" in messages[3] and "Too many" in messages[4]
    locked = c.post("/auth/email/verify", json={"code": right})        # even the right code is refused now
    assert locked.status_code == 429
    assert c.get("/auth/me").json()["user"]["email_verified"] is False


def test_an_expired_code_does_not_work(env):
    c = unverified(env)
    c.post("/auth/email/send")
    with env["factory"]() as s:
        row = s.scalars(select(EmailCode)).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        s.commit()
    r = c.post("/auth/email/verify", json={"code": env["mailer"].last_code()})
    assert r.status_code == 400 and "expired" in r.json()["detail"]


def test_a_new_code_replaces_the_old_one(env):
    c = unverified(env)
    c.post("/auth/email/send")
    first = env["mailer"].last_code()
    skip_cooldown(env)
    c.post("/auth/email/send")
    second = env["mailer"].last_code()
    if first != second:   # (a one-in-a-million repeat would make this check meaningless)
        assert c.post("/auth/email/verify", json={"code": first}).status_code == 400
    assert c.post("/auth/email/verify", json={"code": second}).status_code == 200


def test_a_code_works_only_once(env):
    c = unverified(env)
    c.post("/auth/email/send")
    code = env["mailer"].last_code()
    assert c.post("/auth/email/verify", json={"code": code}).status_code == 200
    assert c.post("/auth/email/verify", json={"code": code}).status_code == 400


def test_a_code_does_not_survive_an_email_change(env):
    c = unverified(env)
    c.post("/auth/email/send")
    code = env["mailer"].last_code()
    with env["factory"]() as s:
        s.scalars(select(User)).one().email = "new@example.com"   # e.g. PayPal reported a different address at sign-in
        s.commit()
    assert c.post("/auth/email/verify", json={"code": code}).status_code == 400


def test_nobody_can_use_someone_elses_code(env):
    ann, ben = unverified(env, "ann@example.com"), unverified(env, "ben@example.com")
    ann.post("/auth/email/send")
    assert ben.post("/auth/email/verify", json={"code": env["mailer"].last_code()}).status_code == 400
    assert ben.get("/auth/me").json()["user"]["email_verified"] is False


def test_the_code_must_look_like_six_digits(env):
    c = unverified(env)
    c.post("/auth/email/send")
    for bad in ("12345", "1234567", "abcdef", ""):
        assert c.post("/auth/email/verify", json={"code": bad}).status_code == 422


# ---- limits ---------------------------------------------------------------------

def test_codes_cannot_be_requested_in_a_burst(env):
    c = unverified(env)
    assert c.post("/auth/email/send").status_code == 200
    r = c.post("/auth/email/send")
    assert r.status_code == 429 and "wait" in r.json()["detail"]
    assert len(env["mailer"].sent) == 1


def test_there_is_an_hourly_cap_too(env):
    auth.send_limiter = RateLimiter(limit=2, window=3600)
    c = unverified(env)
    for _ in range(2):
        assert c.post("/auth/email/send").status_code == 200
        skip_cooldown(env)
    assert c.post("/auth/email/send").status_code == 429


def test_signed_out_visitors_cannot_use_it(env):
    c = TestClient(app)
    assert c.post("/auth/email/send").status_code == 401
    assert c.post("/auth/email/verify", json={"code": "123456"}).status_code == 401


# ---- when email is not set up or breaks -------------------------------------------

def test_without_email_set_up_dev_mode_shows_the_code(env):
    env["mailer"] = None
    c = unverified(env)
    r = c.post("/auth/email/send").json()
    assert r["sent"] is False and len(r["dev_code"]) == 6
    assert c.post("/auth/email/verify", json={"code": r["dev_code"]}).status_code == 200


def test_without_email_set_up_production_says_so_and_never_leaks_the_code(env):
    env["mailer"] = None
    env["settings"] = Settings(public_api_url="https://x.test", frontend_url="https://x.test", dev_login=False)
    c = TestClient(app)
    c.post("/auth/dev-login", json={"name": "A", "email": "a@b.co"})   # disabled in production...
    # ...so sign in through the database directly, as PayPal sign-in would, but with an unconfirmed email.
    from app.models import AuthSession

    with env["factory"]() as s:
        user = User(name="Ann", email="ann@example.com", email_verified=False)
        s.add(user)
        s.commit()
        token = auth.start_session(s, user)
    c.cookies.set(auth.SESSION_COOKIE, token)
    r = c.post("/auth/email/send")
    assert r.status_code == 503 and "dev_code" not in r.text


def test_a_broken_provider_is_a_clean_502_not_a_crash(env):
    env["mailer"].fail = True
    r = unverified(env).post("/auth/email/send")
    assert r.status_code == 502 and "try again" in r.json()["detail"].lower()


# ---- what a verified email unlocks ---------------------------------------------------

def test_reserving_and_opening_a_shop_need_a_verified_email(env):
    c = unverified(env)
    assert c.post("/shop", json={"name": "Ann's"}).status_code == 403
    assert "verify your email" in c.post("/shop", json={"name": "Ann's"}).json()["detail"].lower()
    assert c.post("/drops/1/orders", json={"quantity": 1}).status_code == 403

    c.post("/auth/email/send")
    c.post("/auth/email/verify", json={"code": env["mailer"].last_code()})
    assert c.post("/shop", json={"name": "Ann's"}).status_code == 201


def test_the_assistant_will_not_place_an_order_for_an_unverified_email(env, session):
    from app import agent
    from tests.fakes import ScriptedLLM

    buyer = User(name="Sam", email="sam@example.com", email_verified=False)
    session.add(buyer)
    session.commit()
    llm = ScriptedLLM([("place_order", {"drop_id": 1, "quantity": 1})], "ok")
    agent.run_turn(session, llm, FakePayPal(), SETTINGS, "buyer", "user-1", "order please", user=buyer)
    import json

    result = json.loads(next(m["result"] for m in llm.calls[1]["messages"] if m["role"] == "tool"))
    assert "verify their email" in result["error"]


# ---- the SMTP mailer itself ------------------------------------------------------------

class FakeSmtp:
    log: list = []

    def __init__(self, host, port, timeout=None, context=None):
        FakeSmtp.log.append(("connect", host, port, context is not None))

    def starttls(self, context=None):
        FakeSmtp.log.append(("starttls",))

    def login(self, user, password):
        FakeSmtp.log.append(("login", user))

    def send_message(self, msg):
        FakeSmtp.log.append(("send", msg["To"], msg["Subject"], msg.get_content().strip()))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_smtp_on_587_uses_starttls_and_logs_in(monkeypatch):
    FakeSmtp.log = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSmtp)
    mailer.SmtpMailer("smtp.test", 587, "user", "pw", "hi@fullbatch.test").send("a@b.co", "Subject", "Body")
    assert [e[0] for e in FakeSmtp.log] == ["connect", "starttls", "login", "send"]
    assert FakeSmtp.log[-1] == ("send", "a@b.co", "Subject", "Body")


def test_smtp_on_465_uses_implicit_tls(monkeypatch):
    FakeSmtp.log = []
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSmtp)
    mailer.SmtpMailer("smtp.test", 465, "", "", "hi@fullbatch.test").send("a@b.co", "S", "B")
    assert [e[0] for e in FakeSmtp.log] == ["connect", "send"]   # no starttls, and no login when there is no user


def test_smtp_failures_become_mail_errors_without_leaking_details(monkeypatch):
    class Boom(FakeSmtp):
        def login(self, user, password):
            raise mailer.smtplib.SMTPAuthenticationError(535, b"bad credentials for hunter2")

    monkeypatch.setattr(mailer.smtplib, "SMTP", Boom)
    with pytest.raises(mailer.MailError) as err:
        mailer.SmtpMailer("smtp.test", 587, "user", "hunter2", "x@y.z").send("a@b.co", "S", "B")
    assert "hunter2" not in str(err.value)


def test_no_mailer_unless_email_is_configured():
    assert mailer.from_settings(Settings()) is None
    assert mailer.from_settings(Settings(smtp_host="smtp.test")) is None          # no sender address
    assert mailer.from_settings(Settings(smtp_host="smtp.test", email_from="hi@x.test")) is not None
