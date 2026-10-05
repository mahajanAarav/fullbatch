"""Build the language model the app uses from whichever API keys are in .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

from app import gemini, groq
from app.llm import FallbackLLM


def from_env(order: tuple[str, ...] = ("gemini", "groq")) -> FallbackLLM:
    """
    The providers whose keys are set, in `order`. The default is Gemini first (best quality, but tiny
    free quotas) and Groq as the last resort.
    """
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    makers = {"gemini": ("GEMINI_KEY", gemini.from_env), "groq": ("GROQ_KEY", groq.from_env)}
    providers = [(name, makers[name][1]()) for name in order if os.getenv(makers[name][0])]
    if not providers:
        raise RuntimeError("Set GEMINI_KEY and/or GROQ_KEY in .env.")
    return FallbackLLM(providers)
