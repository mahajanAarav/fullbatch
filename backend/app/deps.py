"""Process-wide helpers that routes receive through FastAPI dependencies (tests replace them)."""

from functools import lru_cache

from app.paypal import from_env as paypal_from_env


@lru_cache
def get_paypal():
    """One PayPal client for the whole process, so the access token is reused."""
    return paypal_from_env()


@lru_cache
def get_llm():
    """The language model the agent talks to (Gemini, then Groq). Imported here so tests need no API keys."""
    from app.llm_factory import from_env as llm_from_env

    return llm_from_env()


@lru_cache
def get_studio_llm():
    """
    The model behind AG Studio's assistant. Groq goes first here because Studio describes its tools with
    richer JSON Schema than Gemini accepts (Gemini would need parts of the schema stripped).
    """
    from app.llm_factory import from_env as llm_from_env

    return llm_from_env(order=("groq", "gemini"))
