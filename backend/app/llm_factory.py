"""Build the language model the app uses from whichever API keys are in .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

from app import gemini, groq
from app.llm import FallbackLLM


def from_env() -> FallbackLLM:
    """Gemini first (best quality, but tiny free quotas), Groq as the last resort."""
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    providers = []
    if os.getenv("GEMINI_KEY"):
        providers.append(("gemini", gemini.from_env()))
    if os.getenv("GROQ_KEY"):
        providers.append(("groq", groq.from_env()))
    if not providers:
        raise RuntimeError("Set GEMINI_KEY and/or GROQ_KEY in .env.")
    return FallbackLLM(providers)
