"""The server side of AG Studio's assistant: a guarded, rate-limited door to our model chain."""

import pytest
from fastapi.testclient import TestClient

from app import studio_ai
from app.config import Settings, get_settings
from app.db import get_session
from app.deps import get_paypal, get_studio_llm
from app.llm import LLMError
from app.main import app
from app.ratelimit import RateLimiter
from tests.fakes import FakePayPal, ScriptedLLM, sign_in

SETTINGS = Settings(public_api_url="http://api.test", frontend_url="http://app.test", dev_login=True)
TOOL = {"name": "view_schema", "description": "List the fields", "parameters": {"type": "object", "properties": {}}}


@pytest.fixture
def make_client(session_factory):
    holder = {"llm": ScriptedLLM("hello there")}

    def _session():
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_paypal] = lambda: FakePayPal()
    app.dependency_overrides[get_studio_llm] = lambda: holder["llm"]
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    studio_ai.limiter = RateLimiter(limit=3, window=600)  # small, so the limit is easy to hit

    def build(shop=True, email="bea@example.com"):
        c = TestClient(app, follow_redirects=False)
        c.holder = holder
        sign_in(c, "Bea", email)
        if shop:
            c.post("/shop", json={"name": "Bea's"})
        return c

    yield build
    app.dependency_overrides.clear()


def turn(c, **overrides):
    body = {"instructions": "You help a seller.", "messages": [{"role": "user", "text": "hi"}], "tools": [TOOL]}
    body.update(overrides)
    return c.post("/ai/turn", json=body)


def test_it_needs_sign_in_and_a_shop(make_client):
    assert TestClient(app).post("/ai/turn", json={"messages": []}).status_code == 401
    assert turn(make_client(shop=False)).status_code == 403


def test_a_text_reply_comes_back(make_client):
    r = turn(make_client())
    assert r.status_code == 200 and r.json() == {"text": "hello there", "tool_calls": []}


def test_tool_requests_come_back_and_the_model_sees_the_conversation(make_client):
    c = make_client()
    c.holder["llm"] = ScriptedLLM([("view_schema", {"x": 1})])
    msgs = [
        {"role": "user", "text": "what fields exist?"},
        {"role": "assistant", "tool_calls": [{"id": "c0", "name": "view_schema", "args": {}}]},
        {"role": "tool", "call_id": "c0", "name": "view_schema", "result": '{"fields": ["a"]}'},
    ]
    r = turn(c, messages=msgs)
    assert r.json()["tool_calls"][0]["name"] == "view_schema" and r.json()["tool_calls"][0]["args"] == {"x": 1}
    seen = c.holder["llm"].calls[0]
    assert seen["system"] == "You help a seller." and [m["role"] for m in seen["messages"]] == ["user", "assistant", "tool"]
    assert seen["tools"][0]["name"] == "view_schema"


def test_a_model_outage_is_a_503(make_client):
    class Down:
        def generate(self, **kw):
            raise LLMError("unreachable")

    c = make_client()
    c.holder["llm"] = Down()
    assert turn(c).status_code == 503


def test_each_person_is_rate_limited_separately(make_client):
    a, b = make_client(email="a@example.com"), make_client(email="b@example.com")
    a.holder["llm"] = ScriptedLLM(*["ok"] * 10)   # plenty of replies: this test is about the limiter
    assert [turn(a).status_code for _ in range(4)] == [200, 200, 200, 429]
    assert "Try again in" in turn(a).json()["detail"]
    assert turn(b).status_code == 200                     # someone else is unaffected


def test_oversized_requests_are_refused(make_client):
    c = make_client()
    assert turn(c, messages=[{"role": "user", "text": "x"}] * 81).status_code == 422       # too many messages
    assert turn(c, tools=[TOOL] * 41).status_code == 422                                    # too many tools
    assert turn(c, messages=[{"role": "user", "text": "x" * 160_000}]).status_code == 413  # too much text


def test_malformed_requests_are_refused(make_client):
    c = make_client()
    assert turn(c, messages=[{"role": "hacker", "text": "hi"}]).status_code == 422
    assert c.post("/ai/turn", json={"nonsense": True}).status_code == 422


def test_the_limiter_forgets_old_calls():
    now = [0.0]
    lim = RateLimiter(limit=2, window=10, clock=lambda: now[0])
    assert [lim.allow("u") for _ in range(3)] == [True, True, False]
    assert lim.retry_after("u") >= 1
    now[0] = 11.0
    assert lim.allow("u") is True
