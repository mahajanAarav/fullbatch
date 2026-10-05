"""
The server side of AG Studio's AI assistant.

AG Studio ships no model adapters. The browser has a small adapter that sends each assistant turn
here. We run it through our own model chain and hand back text or tool requests. API keys never
leave the server, only signed-in sellers can call it, and each person is rate limited.
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth import require_shop
from app.deps import get_studio_llm
from app.models import Seller
from app.ratelimit import RateLimiter

router = APIRouter(prefix="/ai", tags=["ai"])

MAX_MESSAGES = 80
MAX_TOOLS = 40
MAX_TOTAL_CHARS = 150_000  # a few dozen widgets' worth of context, not a way to ship a novel
limiter = RateLimiter(limit=40, window=600)  # 40 assistant turns per person per 10 minutes


class WireCall(BaseModel):
    id: str = Field(max_length=200)
    name: str = Field(max_length=100)
    args: dict = Field(default_factory=dict)


class WireMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    text: str | None = None
    tool_calls: list[WireCall] = Field(default_factory=list)
    call_id: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, max_length=100)
    result: str | None = None


class WireTool(BaseModel):
    name: str = Field(max_length=100)
    description: str = Field(default="", max_length=4000)
    parameters: dict


class TurnIn(BaseModel):
    instructions: str = Field(default="", max_length=30_000)
    messages: list[WireMessage] = Field(max_length=MAX_MESSAGES)
    tools: list[WireTool] = Field(default_factory=list, max_length=MAX_TOOLS)


@router.post("/turn")
def turn(body: TurnIn, shop: Seller = Depends(require_shop), llm=Depends(get_studio_llm)):
    if not limiter.allow(shop.user_id):
        raise HTTPException(429, f"The assistant is getting a lot of use. Try again in {limiter.retry_after(shop.user_id)} seconds.")

    size = len(body.instructions) + sum(len(m.text or "") + len(m.result or "") for m in body.messages)
    if size > MAX_TOTAL_CHARS:
        raise HTTPException(413, "That conversation is too long for the assistant. Start a new chat.")

    reply = llm.generate(
        system=body.instructions,
        messages=[m.model_dump(exclude_none=True) for m in body.messages],
        tools=[t.model_dump() for t in body.tools],
    )
    return {"text": reply.get("text"), "tool_calls": reply.get("tool_calls") or []}
