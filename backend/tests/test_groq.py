"""The Groq adapter and the provider-level fallback, with fake HTTP (no network, no keys)."""

import json

import httpx
import pytest

from app.gemini import SKIP_SIGNATURE_CHECK, to_contents
from app.groq import GroqClient, parse_reply, to_messages
from app.llm import FallbackLLM, LLMError

TOOLS = [{"name": "get_stock", "description": "d", "parameters": {"type": "object", "properties": {}}}]
MSG = [{"role": "user", "text": "hi"}]


def client_for(handler, sleeps=None, models="m", clock=None):
    return GroqClient("key", models=models, transport=httpx.MockTransport(handler),
                      sleep=(sleeps.append if sleeps is not None else lambda s: None),
                      **({"clock": clock} if clock else {}))


def completion(message):
    return httpx.Response(200, json={"choices": [{"message": message}]})


def model_of(request):
    return json.loads(request.content)["model"]


# ---- translation -------------------------------------------------------------

def test_messages_translate_to_openai_style():
    out = to_messages([
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": None, "tool_calls": [{"id": "c1", "name": "get_stock", "args": {"drop_id": 1}, "signature": "GEMINI-ONLY"}]},
        {"role": "tool", "call_id": "c1", "name": "get_stock", "result": '{"left": 3}'},
        {"role": "assistant", "text": "3 left", "tool_calls": []},
        {"role": "assistant", "text": None, "tool_calls": []},  # empty: dropped
    ])
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "assistant"]
    call = out[1]["tool_calls"][0]
    assert call == {"id": "c1", "type": "function", "function": {"name": "get_stock", "arguments": '{"drop_id": 1}'}}
    assert out[2] == {"role": "tool", "tool_call_id": "c1", "content": '{"left": 3}'}
    assert "GEMINI-ONLY" not in json.dumps(out)


def test_text_and_tool_call_replies():
    assert parse_reply({"choices": [{"message": {"content": " Hello "}}]}) == {"text": "Hello", "tool_calls": []}
    out = parse_reply({"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "fc_1", "type": "function", "function": {"name": "get_stock", "arguments": '{"drop_id":7}'}}]}}]})
    assert out == {"text": None, "tool_calls": [{"id": "fc_1", "name": "get_stock", "args": {"drop_id": 7}}]}


def test_garbled_tool_arguments_become_empty_args_not_a_crash():
    out = parse_reply({"choices": [{"message": {"tool_calls": [
        {"id": "x", "function": {"name": "get_stock", "arguments": "{not json"}}]}}]})
    assert out["tool_calls"][0]["args"] == {}


def test_unexpected_response_is_an_llm_error():
    with pytest.raises(LLMError):
        parse_reply({"nope": 1})


# ---- the HTTP call -------------------------------------------------------------

def test_request_shape_and_key_in_header_only():
    seen = []

    def handler(request):
        seen.append(request)
        return completion({"content": "ok"})

    out = client_for(handler).generate(system="be nice", messages=MSG, tools=TOOLS)
    assert out["text"] == "ok"
    req, body = seen[0], json.loads(seen[0].content)
    assert req.headers["authorization"] == "Bearer key" and "key" not in str(req.url)
    assert body["messages"][0] == {"role": "system", "content": "be nice"}
    assert body["tools"][0] == {"type": "function", "function": {"name": "get_stock", "description": "d", "parameters": TOOLS[0]["parameters"]}}


def test_429_waits_as_long_as_groq_says_then_succeeds():
    answers = [httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "tpm"}}), completion({"content": "ok"})]
    sleeps = []
    assert client_for(lambda r: answers.pop(0), sleeps).generate(system="s", messages=MSG, tools=[])["text"] == "ok"
    assert sleeps == [7.0]


def test_a_long_wait_moves_to_the_next_model_and_remembers():
    used = []

    def handler(request):
        used.append(model_of(request))
        if model_of(request) == "a":
            return httpx.Response(429, headers={"retry-after": "3600"}, json={"error": {"message": "daily"}})
        return completion({"content": "from b"})

    now = [0.0]
    client = client_for(handler, models=["a", "b"], clock=lambda: now[0])
    assert client.generate(system="s", messages=MSG, tools=[])["text"] == "from b"
    assert client.generate(system="s", messages=MSG, tools=[])["text"] == "from b"
    assert used == ["a", "b", "b"]


def test_removed_or_overloaded_models_are_skipped():
    def handler(request):
        return {"gone": httpx.Response(404, text="no"), "busy": httpx.Response(503, text="busy")}.get(
            model_of(request)) or completion({"content": "ok"})

    assert client_for(handler, models=["gone", "busy", "fine"]).generate(system="s", messages=MSG, tools=[])["text"] == "ok"


def test_our_own_bad_request_is_not_hidden():
    used = []

    def handler(request):
        used.append(model_of(request))
        return httpx.Response(400, json={"error": {"message": "bad body"}})

    with pytest.raises(LLMError, match="bad body"):
        client_for(handler, models=["a", "b"]).generate(system="s", messages=MSG, tools=[])
    assert used == ["a"]


def test_requires_a_key():
    with pytest.raises(ValueError):
        GroqClient("")


# ---- provider-level fallback ---------------------------------------------------

class Stub:
    def __init__(self, result=None, fail=False):
        self.result, self.fail, self.calls = result or {"text": "ok", "tool_calls": []}, fail, 0

    def generate(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise LLMError("down")
        return self.result


def ask(llm):
    return llm.generate(system="s", messages=MSG, tools=[])


def test_first_working_provider_answers():
    a, b = Stub({"text": "from a", "tool_calls": []}), Stub()
    assert ask(FallbackLLM([("a", a), ("b", b)]))["text"] == "from a" and b.calls == 0


def test_a_failed_provider_is_skipped_and_rests():
    a, b = Stub(fail=True), Stub({"text": "from b", "tool_calls": []})
    now = [0.0]
    llm = FallbackLLM([("a", a), ("b", b)], cooldown=60, clock=lambda: now[0])
    assert ask(llm)["text"] == "from b" and ask(llm)["text"] == "from b"
    assert a.calls == 1  # not retried while resting
    now[0] = 61.0
    ask(llm)
    assert a.calls == 2  # tried again after the rest


def test_all_failing_names_every_provider():
    with pytest.raises(LLMError) as err:
        ask(FallbackLLM([("a", Stub(fail=True)), ("b", Stub(fail=True))]))
    assert "a: down" in str(err.value) and "b: down" in str(err.value)


def test_if_every_provider_is_resting_they_are_all_tried_anyway():
    a, b = Stub(fail=True), Stub(fail=True)
    llm = FallbackLLM([("a", a), ("b", b)])
    with pytest.raises(LLMError):
        ask(llm)
    b.fail = False
    assert ask(llm)["text"] == "ok"


def test_needs_at_least_one_provider():
    with pytest.raises(ValueError):
        FallbackLLM([])


# ---- switching providers mid-conversation --------------------------------------

def test_gemini_accepts_a_tool_call_that_came_from_groq():
    """Groq's calls carry no thought signature; Gemini would 400 without the bypass value."""
    contents = to_contents([
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": None, "tool_calls": [
            {"id": "c1", "name": "get_stock", "args": {}}, {"id": "c2", "name": "get_stock", "args": {}}]},
    ])
    first, second = contents[1]["parts"]
    assert first["thoughtSignature"] == SKIP_SIGNATURE_CHECK
    assert "thoughtSignature" not in second  # only the first call of a group needs one
