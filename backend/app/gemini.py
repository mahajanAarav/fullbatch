"""
Gemini adapter: translates the agent's neutral messages (see app/llm.py) to
Google's generateContent API and back.

Notes from the real API:
  - generateContent is stateless: we resend the whole history each call, which
    suits us because we store the conversation ourselves.
  - Gemini 3 models return a "thought signature" on tool calls and REFUSE the
    next request (HTTP 400) if it is not sent back. We keep it in the tool
    call's "signature" field, which the agent stores with the conversation.
  - Results for several parallel tool calls must share one user turn.
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

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Free-tier quotas are counted PER MODEL (about 20 requests a day each), so when one
# model runs out we move to the next. Best first. Tested: Gemini accepts a tool
# exchange that started on one model being continued on another.
DEFAULT_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
)
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1.0, 3.0)  # seconds to wait before the 2nd and 3rd attempt
MAX_RETRY_WAIT = 20.0      # never keep a chatting user waiting longer than this for a retry


class _Skip(Exception):
    """This model cannot answer right now. Try the next one."""

    def __init__(self, reason: str, block_for: float = 0.0):
        super().__init__(reason)
        self.reason = reason
        self.block_for = block_for  # seconds to leave this model alone


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        models: str | tuple[str, ...] | list[str] = DEFAULT_MODELS,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        if not api_key:
            raise ValueError("A Gemini API key is required (GEMINI_KEY).")
        self._models = [models] if isinstance(models, str) else list(models)
        if not self._models:
            raise ValueError("At least one Gemini model is required.")
        self._blocked_until: dict[str, float] = {}  # model -> when to try it again
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"x-goog-api-key": api_key},  # in a header, never in the URL
            timeout=60,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock

    # ---- the interface the agent uses ------------------------------------

    def generate(self, *, system: str, messages: list[dict], tools: list[dict]) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": to_contents(messages),
        }
        if tools:
            declarations = [{**t, "parameters": to_gemini_schema(t["parameters"])} for t in tools]
            body["tools"] = [{"functionDeclarations": declarations}]

        now = self._clock()
        available = [m for m in self._models if self._blocked_until.get(m, 0) <= now]
        # If every model is cooling down, still try them all rather than refuse to answer.
        failures: list[str] = []
        for model in available or self._models:
            try:
                return parse_reply(self._post(model, body))
            except _Skip as skip:
                failures.append(f"{model}: {skip.reason}")
                if skip.block_for:
                    self._blocked_until[model] = self._clock() + skip.block_for
                log.warning("Gemini model %s skipped (%s); trying the next one", model, skip.reason)
        raise LLMError("Gemini request failed (" + "; ".join(failures) + ")")

    # ---- plumbing ---------------------------------------------------------

    def _post(self, model: str, body: dict) -> dict:
        """One model, with short retries for temporary trouble. Raises _Skip to move on."""
        path = f"/models/{model}:generateContent"
        last = "no response"
        for attempt in range(len(RETRY_DELAYS) + 1):
            wait = RETRY_DELAYS[attempt] if attempt < len(RETRY_DELAYS) else 0.0
            try:
                r = self._http.post(path, json=body)
            except httpx.HTTPError as err:
                last = f"network error: {type(err).__name__}"
            else:
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {_error_message(r)}"
                if r.status_code == 404:
                    raise _Skip(last, block_for=3600)  # model removed or not available to us
                if r.status_code == 429:
                    suggested = _retry_after(r)
                    if _is_daily_quota(r) or (suggested or 0) > MAX_RETRY_WAIT:
                        raise _Skip("daily quota used up", block_for=suggested or 3600)
                    wait = suggested or wait  # per-minute quota: wait exactly as long as Google asks
                elif r.status_code not in RETRY_STATUSES:
                    raise LLMError(f"Gemini request failed ({last})")  # our own bad request: do not hide it
            if attempt < len(RETRY_DELAYS):
                self._sleep(min(wait, MAX_RETRY_WAIT))
        raise _Skip(last, block_for=60)  # still failing after retries: rest this model briefly

    def close(self) -> None:
        self._http.close()


def _is_daily_quota(r: httpx.Response) -> bool:
    try:
        for detail in r.json()["error"].get("details", []):
            for violation in detail.get("violations", []):
                if "PerDay" in violation.get("quotaId", ""):
                    return True
    except Exception:
        pass
    return False


def _retry_after(r: httpx.Response) -> float | None:
    """Google's suggested wait on a 429, e.g. {"@type": ".../RetryInfo", "retryDelay": "12s"}."""
    try:
        for detail in r.json()["error"].get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                return float(str(detail["retryDelay"]).rstrip("s"))
    except Exception:
        pass
    return None


def _error_message(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"]["message"])[:300]
    except Exception:
        return r.text[:300]


# Gemini accepts only a small subset of JSON Schema and returns HTTP 400 for anything
# else (e.g. exclusiveMinimum, pattern, maxLength). Our tool code still enforces those
# rules itself and returns a readable error if the model breaks one.
_SCHEMA_KEYS = {"type", "description", "properties", "required", "enum", "items"}


def to_gemini_schema(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key not in _SCHEMA_KEYS:
                continue
            if key == "properties":  # property NAMES are free-form, only their values are schemas
                out[key] = {name: to_gemini_schema(sub) for name, sub in value.items()}
            else:
                out[key] = to_gemini_schema(value) if key == "items" else value
        return out
    return node


# ---- translation: our messages -> Gemini contents ---------------------------

def to_contents(messages: list[dict]) -> list[dict]:
    contents: list[dict] = []

    def add(role: str, parts: list[dict]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)  # Gemini wants one turn per speaker in a row
        else:
            contents.append({"role": role, "parts": parts})

    for m in messages:
        if m["role"] == "user":
            add("user", [{"text": m["text"]}])
        elif m["role"] == "assistant":
            parts: list[dict] = []
            if m.get("text"):
                parts.append({"text": m["text"]})
            for call in m.get("tool_calls") or []:
                part = {"functionCall": {"id": call["id"], "name": call["name"], "args": call.get("args") or {}}}
                if call.get("signature"):
                    part["thoughtSignature"] = call["signature"]
                parts.append(part)
            add("model", parts)
        elif m["role"] == "tool":
            add("user", [{"functionResponse": {"id": m["call_id"], "name": m["name"], "response": _as_object(m["result"])}}])
    return contents


def _as_object(result: str) -> dict:
    """Gemini wants the tool result as a JSON object."""
    try:
        value = json.loads(result)
    except (TypeError, ValueError):
        return {"result": result}
    return value if isinstance(value, dict) else {"result": value}


# ---- translation: Gemini reply -> our reply ---------------------------------

def parse_reply(data: dict) -> dict:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
        raise LLMError(f"Gemini returned no answer ({reason}).")
    parts = (candidates[0].get("content") or {}).get("parts") or []

    text_bits: list[str] = []
    calls: list[dict] = []
    for part in parts:
        if part.get("thought"):
            continue  # a summary of the model's reasoning, not meant for the user
        if "functionCall" in part:
            fc = part["functionCall"]
            call = {
                "id": fc.get("id") or f"call_{uuid.uuid4().hex[:10]}",
                "name": fc["name"],
                "args": fc.get("args") or {},
            }
            if part.get("thoughtSignature"):
                call["signature"] = part["thoughtSignature"]
            calls.append(call)
        elif part.get("text"):
            text_bits.append(part["text"])
    return {"text": "".join(text_bits).strip() or None, "tool_calls": calls}


def from_env() -> GeminiClient:
    """GEMINI_KEY is required. GEMINI_MODEL is optional: one model, or a comma-separated fallback list."""
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    chosen = [m.strip() for m in os.getenv("GEMINI_MODEL", "").split(",") if m.strip()]
    return GeminiClient(os.getenv("GEMINI_KEY", ""), chosen or DEFAULT_MODELS)
