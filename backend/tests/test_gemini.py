"""The Gemini adapter, with the HTTP layer faked (no network, no key)."""

import json

import httpx
import pytest

from app import agent
from app.gemini import GeminiClient, parse_reply, to_contents, to_gemini_schema
from app.llm import LLMError

TOOLS = [{"name": "get_stock", "description": "d", "parameters": {"type": "object", "properties": {}}}]


def client_for(handler, sleeps=None, models="gemini-test", clock=None):
    return GeminiClient(
        "key", models=models, transport=httpx.MockTransport(handler),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        **({"clock": clock} if clock else {}),
    )


def reply(parts, status=200):
    return httpx.Response(status, json={"candidates": [{"content": {"role": "model", "parts": parts}}]})


# ---- messages -> contents ---------------------------------------------------

def test_conversation_translation_including_signature_and_parallel_results():
    messages = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": None, "tool_calls": [
            {"id": "a", "name": "get_stock", "args": {"drop_id": 1}, "signature": "SIG"},
            {"id": "b", "name": "get_stock", "args": {"drop_id": 2}},
        ]},
        {"role": "tool", "call_id": "a", "name": "get_stock", "result": json.dumps({"left": 3})},
        {"role": "tool", "call_id": "b", "name": "get_stock", "result": json.dumps({"left": 0})},
        {"role": "assistant", "text": "Drop 1 has 3.", "tool_calls": []},
    ]
    contents = to_contents(messages)
    assert [c["role"] for c in contents] == ["user", "model", "user", "model"]
    call_a, call_b = contents[1]["parts"]
    assert call_a["thoughtSignature"] == "SIG" and "thoughtSignature" not in call_b
    assert call_a["functionCall"] == {"id": "a", "name": "get_stock", "args": {"drop_id": 1}}
    results = contents[2]["parts"]  # both results share ONE user turn
    assert [r["functionResponse"]["id"] for r in results] == ["a", "b"]
    assert results[0]["functionResponse"]["response"] == {"left": 3}


def test_consecutive_user_messages_are_merged_and_empty_ones_dropped():
    contents = to_contents([
        {"role": "user", "text": "one"},
        {"role": "user", "text": "two"},
        {"role": "assistant", "text": None, "tool_calls": []},
    ])
    assert contents == [{"role": "user", "parts": [{"text": "one"}, {"text": "two"}]}]


def test_non_object_tool_results_are_wrapped():
    contents = to_contents([{"role": "tool", "call_id": "a", "name": "t", "result": "plain text"}])
    assert contents[0]["parts"][0]["functionResponse"]["response"] == {"result": "plain text"}


# ---- reply -> our format ----------------------------------------------------

def test_text_reply():
    assert parse_reply({"candidates": [{"content": {"parts": [{"text": "Hello "}, {"text": "there"}]}}]}) == {
        "text": "Hello there", "tool_calls": []}


def test_tool_call_reply_keeps_the_signature():
    out = parse_reply({"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "get_stock", "args": {"drop_id": 7}, "id": "call_1"}, "thoughtSignature": "SIG"}]}}]})
    assert out == {"text": None, "tool_calls": [{"id": "call_1", "name": "get_stock", "args": {"drop_id": 7}, "signature": "SIG"}]}


def test_missing_call_id_is_filled_in_and_thoughts_are_hidden():
    out = parse_reply({"candidates": [{"content": {"parts": [
        {"text": "private reasoning", "thought": True},
        {"functionCall": {"name": "get_stock"}},
        {"text": "visible"}]}}]})
    assert out["text"] == "visible"
    assert out["tool_calls"][0]["id"].startswith("call_") and out["tool_calls"][0]["args"] == {}


def test_blocked_prompt_is_an_llm_error():
    with pytest.raises(LLMError, match="SAFETY"):
        parse_reply({"promptFeedback": {"blockReason": "SAFETY"}})


# ---- the HTTP call ------------------------------------------------------------

def test_request_shape_and_key_is_in_header_not_url():
    seen = []

    def handler(request):
        seen.append(request)
        return reply([{"text": "ok"}])

    out = client_for(handler).generate(system="be nice", messages=[{"role": "user", "text": "hi"}], tools=TOOLS)
    assert out["text"] == "ok"
    req = seen[0]
    assert req.headers["x-goog-api-key"] == "key" and "key" not in str(req.url)
    assert req.url.path.endswith(":generateContent")
    body = json.loads(req.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "be nice"
    assert body["tools"] == [{"functionDeclarations": TOOLS}]


def test_no_tools_key_when_there_are_no_tools():
    seen = []
    client_for(lambda r: (seen.append(json.loads(r.content)), reply([{"text": "ok"}]))[1]).generate(
        system="s", messages=[{"role": "user", "text": "hi"}], tools=[])
    assert "tools" not in seen[0]


def test_temporary_errors_are_retried_then_succeed():
    answers = [httpx.Response(503, text="busy"), httpx.Response(429, text="slow down"), reply([{"text": "finally"}])]
    sleeps = []
    out = client_for(lambda r: answers.pop(0), sleeps).generate(system="s", messages=[{"role": "user", "text": "hi"}], tools=[])
    assert out["text"] == "finally" and sleeps == [1.0, 3.0]


def test_persistent_failure_becomes_llm_error():
    with pytest.raises(LLMError, match="HTTP 503"):
        client_for(lambda r: httpx.Response(503, text="busy")).generate(system="s", messages=[{"role": "user", "text": "hi"}], tools=[])


def test_client_errors_are_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad request body"}})

    with pytest.raises(LLMError, match="bad request body"):
        client_for(handler).generate(system="s", messages=[{"role": "user", "text": "hi"}], tools=[])
    assert len(calls) == 1


def test_network_failure_becomes_llm_error():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(LLMError, match="network error"):
        client_for(handler).generate(system="s", messages=[{"role": "user", "text": "hi"}], tools=[])


def test_requires_a_key():
    with pytest.raises(ValueError):
        GeminiClient("")


# ---- schema sanitising --------------------------------------------------------

ALLOWED = {"type", "description", "properties", "required", "enum", "items"}


def keys_used(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "properties":
                for sub in v.values():
                    keys_used(sub, found)
            else:
                found.add(k)
                keys_used(v, found)
    elif isinstance(node, list):
        for v in node:
            keys_used(v, found)
    return found


def test_every_real_tool_schema_only_uses_keywords_gemini_accepts():
    for tools in agent.TOOLS_BY_ROLE.values():
        for t in tools:
            cleaned = to_gemini_schema(t.spec()["parameters"])
            assert keys_used(cleaned) <= ALLOWED, (t.name, keys_used(cleaned) - ALLOWED)
            assert cleaned["type"] == "object"


def test_schema_cleaning_keeps_names_types_and_required():
    raw = {"type": "object", "required": ["n"], "properties": {
        "n": {"type": "integer", "exclusiveMinimum": 0, "description": "count"},
        "title": {"type": "string", "pattern": "^x$", "maxLength": 5}}}
    assert to_gemini_schema(raw) == {"type": "object", "required": ["n"], "properties": {
        "n": {"type": "integer", "description": "count"}, "title": {"type": "string"}}}  # a property NAMED "title" survives


def test_a_429_waits_as_long_as_google_asks():
    quota = httpx.Response(429, json={"error": {"message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "12s"}]}})
    answers = [quota, reply([{"text": "ok"}])]
    sleeps = []
    out = client_for(lambda r: answers.pop(0), sleeps).generate(system="s", messages=[{"role": "user", "text": "hi"}], tools=[])
    assert out["text"] == "ok" and sleeps == [12.0]


def test_an_hour_long_retry_delay_means_move_on_not_wait():
    quota = httpx.Response(429, json={"error": {"message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"}]}})
    sleeps = []
    with pytest.raises(LLMError, match="daily quota"):
        client_for(lambda r: quota, sleeps).generate(system="s", messages=[{"role": "user", "text": "hi"}], tools=[])
    assert sleeps == []  # never sat there waiting


# ---- model fallback -----------------------------------------------------------

def daily_quota_response(delay="77000s"):
    return httpx.Response(429, json={"error": {"message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


def model_of(request):
    return request.url.path.split("/models/")[1].split(":")[0]


def test_daily_quota_moves_to_the_next_model_and_remembers():
    used = []

    def handler(request):
        used.append(model_of(request))
        return daily_quota_response() if model_of(request) == "model-a" else reply([{"text": "from b"}])

    now = [0.0]
    client = client_for(handler, models=["model-a", "model-b"], clock=lambda: now[0])
    msg = [{"role": "user", "text": "hi"}]
    assert client.generate(system="s", messages=msg, tools=[])["text"] == "from b"
    assert client.generate(system="s", messages=msg, tools=[])["text"] == "from b"
    assert used == ["model-a", "model-b", "model-b"]  # model-a was not wasted on a second request


def test_an_exhausted_model_is_tried_again_after_its_reset():
    state = {"a_works": False}
    used = []

    def handler(request):
        used.append(model_of(request))
        if model_of(request) == "model-a" and not state["a_works"]:
            return daily_quota_response(delay="100s")
        return reply([{"text": model_of(request)}])

    now = [0.0]
    client = client_for(handler, models=["model-a", "model-b"], clock=lambda: now[0])
    msg = [{"role": "user", "text": "hi"}]
    assert client.generate(system="s", messages=msg, tools=[])["text"] == "model-b"
    state["a_works"], now[0] = True, 101.0
    assert client.generate(system="s", messages=msg, tools=[])["text"] == "model-a"


def test_a_removed_model_is_skipped():
    def handler(request):
        return httpx.Response(404, text="no longer available") if model_of(request) == "old" else reply([{"text": "new works"}])

    assert client_for(handler, models=["old", "new"]).generate(
        system="s", messages=[{"role": "user", "text": "hi"}], tools=[])["text"] == "new works"


def test_an_overloaded_model_falls_through_after_its_retries():
    used = []

    def handler(request):
        used.append(model_of(request))
        return httpx.Response(503, text="busy") if model_of(request) == "a" else reply([{"text": "b ok"}])

    out = client_for(handler, models=["a", "b"]).generate(system="s", messages=[{"role": "user", "text": "hi"}], tools=[])
    assert out["text"] == "b ok" and used == ["a", "a", "a", "b"]  # 3 tries on a, then b


def test_our_own_bad_request_is_not_hidden_by_falling_back():
    used = []

    def handler(request):
        used.append(model_of(request))
        return httpx.Response(400, json={"error": {"message": "bad body"}})

    with pytest.raises(LLMError, match="bad body"):
        client_for(handler, models=["a", "b"]).generate(system="s", messages=[{"role": "user", "text": "hi"}], tools=[])
    assert used == ["a"]


def test_when_every_model_fails_the_error_names_each_one():
    with pytest.raises(LLMError) as err:
        client_for(lambda r: daily_quota_response(), models=["a", "b"]).generate(
            system="s", messages=[{"role": "user", "text": "hi"}], tools=[])
    assert "a: daily quota" in str(err.value) and "b: daily quota" in str(err.value)


def test_a_cooling_down_model_is_still_tried_if_all_are_cooling_down():
    now = [0.0]
    answers = {"a": daily_quota_response(), "b": daily_quota_response()}
    client = client_for(lambda r: answers[model_of(r)], models=["a", "b"], clock=lambda: now[0])
    msg = [{"role": "user", "text": "hi"}]
    with pytest.raises(LLMError):
        client.generate(system="s", messages=msg, tools=[])
    answers["b"] = reply([{"text": "b is back"}])  # recovered earlier than predicted
    assert client.generate(system="s", messages=msg, tools=[])["text"] == "b is back"
