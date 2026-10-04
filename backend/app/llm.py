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

from typing import Protocol


class LLMError(Exception):
    """The model could not be reached or answered with something unusable."""


class LLM(Protocol):
    def generate(self, *, system: str, messages: list[dict], tools: list[dict]) -> dict: ...
