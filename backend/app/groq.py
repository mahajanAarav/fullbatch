"""
Groq adapter (OpenAI-style chat completions): the last-resort fallback model.

Real limits on this account (from response headers): 1,000 requests a day but
only 8,000 tokens a minute, so a busy minute gets a 429 and we wait it out.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.llm import LLMError

log = logging.getLogger(__name__)

BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b")
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1.0, 3.0)
MAX_RETRY_WAIT = 20.0


class _Skip(Exception):
    def __init__(self, reason: str, block_for: float = 0.0):
        super().__init__(reason)
        self.reason = reason
        self.block_for = block_for


class GroqClient:
    def __init__(
        self,
        api_key: str,
        models: str | tuple[str, ...] | list[str] = DEFAULT_MODELS,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if not api_key:
            raise ValueError("A Groq API key is required (GROQ_KEY).")
        self._models = [models] if isinstance(models, str) else list(models)
        if not self._models:
            raise ValueError("At least one Groq model is required.")
        self._blocked_until: dict[str, float] = {}
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},  # header only, never in the URL
            timeout=60,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock

    def generate(self, *, system: str, messages: list[dict], tools: list[dict]) -> dict:
        body = {"messages": [{"role": "system", "content": system}, *to_messages(messages)]}
        if tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                for t in tools
            ]

        now = self._clock()
        available = [m for m in self._models if self._blocked_until.get(m, 0) <= now]
        failures: list[str] = []
        for model in available or self._models:
            try:
                return parse_reply(self._post(model, body))
            except _Skip as skip:
                failures.append(f"{model}: {skip.reason}")
                if skip.block_for:
                    self._blocked_until[model] = self._clock() + skip.block_for
                log.warning("Groq model %s skipped (%s); trying the next one", model, skip.reason)
        raise LLMError("Groq request failed (" + "; ".join(failures) + ")")

    def _post(self, model: str, body: dict) -> dict:
        last = "no response"
        for attempt in range(len(RETRY_DELAYS) + 1):
            wait = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else 0.0
            try:
                r = self._http.post("/chat/completions", json={**body, "model": model})
            except httpx.HTTPError as err:
                last = f"network error: {type(err).__name__}"
            else:
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {_error_message(r)}"
                if r.status_code == 404:
                    raise _Skip(last, block_for=3600)
                if r.status_code == 429:
                    suggested = _retry_after(r)
                    if (suggested or 0) > MAX_RETRY_WAIT:
                        raise _Skip("rate limit, long wait", block_for=suggested)
                    wait = suggested or wait
                elif r.status_code not in RETRY_STATUSES:
                    raise LLMError(f"Groq request failed ({last})")
            if attempt < len(RETRY_DELAYS):
                self._sleep(min(wait, MAX_RETRY_WAIT))
        raise _Skip(last, block_for=60)

    def close(self) -> None:
        self._http.close()


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return float(r.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _error_message(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"]["message"])[:300]
    except Exception:
        return r.text[:300]


def to_messages(messages: list[dict]) -> list[dict]:
    """Our neutral messages -> OpenAI-style chat messages (the system prompt is added by the caller)."""
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": m.get("text")}
            calls = m.get("tool_calls") or []
            if calls:
                msg["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {})}}
                    for c in calls
                ]
            elif not msg["content"]:
                continue  # an empty assistant message helps nobody
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["call_id"], "content": m["result"]})
    return out


def parse_reply(data: dict) -> dict:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise LLMError("Groq returned an unexpected response.")
    calls = []
    for tc in message.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except ValueError:
            args = {}  # the agent's validation will report the missing arguments to the model
        calls.append({
            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:10]}",
            "name": tc["function"]["name"],
            "args": args if isinstance(args, dict) else {},
        })
    return {"text": (message.get("content") or "").strip() or None, "tool_calls": calls}


def from_env() -> GroqClient:
    """GROQ_KEY is required. GROQ_MODEL is optional: one model, or a comma-separated fallback list."""
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    chosen = [m.strip() for m in os.getenv("GROQ_MODEL", "").split(",") if m.strip()]
    return GroqClient(os.getenv("GROQ_KEY", ""), chosen or DEFAULT_MODELS)
