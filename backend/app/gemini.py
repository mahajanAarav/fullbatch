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
import os
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv

from app.llm import LLMError

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.5-flash"
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (1.0, 3.0)  # seconds to wait before the 2nd and 3rd attempt
MAX_RETRY_WAIT = 20.0      # never keep a chatting user waiting longer than this for a retry


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        if not api_key:
            raise ValueError("A Gemini API key is required (GEMINI_KEY).")
        self._model = model
        self._http = httpx.Client(
            base_url=BASE_URL,
            headers={"x-goog-api-key": api_key},  # in a header, never in the URL
            timeout=60,
            transport=transport,
        )
        self._sleep = sleep

    # ---- the interface the agent uses ------------------------------------

    def generate(self, *, system: str, messages: list[dict], tools: list[dict]) -> dict:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": to_contents(messages),
        }
        if tools:
            declarations = [{**t, "parameters": to_gemini_schema(t["parameters"])} for t in tools]
            body["tools"] = [{"functionDeclarations": declarations}]
        data = self._post(body)
        return parse_reply(data)

    # ---- plumbing ---------------------------------------------------------

    def _post(self, body: dict) -> dict:
        path = f"/models/{self._model}:generateContent"
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
                if r.status_code not in RETRY_STATUSES:
                    break
                if r.status_code == 429:
                    # Per-minute quota: when Google says how long to wait, do exactly that.
                    wait = _retry_after(r) or wait
            if attempt < len(RETRY_DELAYS):
                self._sleep(min(wait, MAX_RETRY_WAIT))
        raise LLMError(f"Gemini request failed ({last})")

    def close(self) -> None:
        self._http.close()


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
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    return GeminiClient(os.getenv("GEMINI_KEY", ""), os.getenv("GEMINI_MODEL") or DEFAULT_MODEL)
