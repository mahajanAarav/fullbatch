"""
The one interface the agent needs from a language model.

The agent speaks a small provider-neutral format. A provider adapter (e.g. the
Gemini one) translates it to that provider's API, so swapping models never
touches the agent or its tests.

Messages (what the agent sends):
  {"role": "user",      "text": "..."}
  {"role": "assistant", "text": "..." or None, "tool_calls": [{"id", "name", "args"}]}
  {"role": "tool",      "call_id": "...", "name": "...", "result": "<json string>"}

Tools (what the agent offers):
  {"name": "...", "description": "...", "parameters": <JSON schema object>}

generate() returns:
  {"text": "..." or None, "tool_calls": [{"id", "name", "args"}]}
"""

import logging
import time
from typing import Protocol

log = logging.getLogger(__name__)


class LLMError(Exception):
    """The model could not be reached or answered with something unusable."""


class LLM(Protocol):
    def generate(self, *, system: str, messages: list[dict], tools: list[dict]) -> dict: ...


class FallbackLLM:
    """
    Try several providers in order (e.g. Gemini, then Groq). A provider that just
    failed rests for `cooldown` seconds so every chat message is not slowed down
    by retrying something that is down. If all are resting, all are tried anyway.
    """

    def __init__(self, providers: list[tuple[str, LLM]], cooldown: float = 60.0, clock=time.monotonic):
        if not providers:
            raise ValueError("At least one language model provider is required.")
        self._providers = providers
        self._cooldown = cooldown
        self._clock = clock
        self._resting_until: dict[str, float] = {}

    def generate(self, *, system: str, messages: list[dict], tools: list[dict]) -> dict:
        now = self._clock()
        ready = [p for p in self._providers if self._resting_until.get(p[0], 0) <= now]
        failures: list[str] = []
        for name, provider in ready or self._providers:
            try:
                return provider.generate(system=system, messages=messages, tools=tools)
            except LLMError as err:
                failures.append(f"{name}: {err}")
                self._resting_until[name] = self._clock() + self._cooldown
                log.warning("Language model provider %s failed; trying the next one", name)
        raise LLMError("All language model providers failed (" + "; ".join(failures) + ")")
