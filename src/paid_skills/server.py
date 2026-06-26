"""HTTP wrapper — expose PaidSkillRuntime as a FastAPI service.

Implements the minimum endpoints a deployment needs. Add auth + Stripe webhook
handling here in production (or fork this file and add what's missing).

Run with:
    ANTHROPIC_API_KEY=... uvicorn paid_skills.server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .pricing import calculate_price, format_price
from .runtime import PaidSkillRuntime, PaymentRequired, SkillInvocation
from .skill_loader import SkillLoadError, load_skill


logger = logging.getLogger(__name__)
app = FastAPI(
    title="paid-skills",
    description="Open-source runtime for monetizing Claude Skills",
    version="0.1.0",
)

_runtime: PaidSkillRuntime | None = None


def get_runtime() -> PaidSkillRuntime:
    global _runtime
    if _runtime is None:
        _runtime = PaidSkillRuntime()
    return _runtime


# ----- Schemas --------------------------------------------------------------


class InvokeRequest(BaseModel):
    skill_path: str = Field(..., description="Server-side path to a skill directory.")
    user_message: str = Field(..., description="The user's request.")
    model: str = Field("claude-sonnet-4-6", description="Anthropic model id.")
    max_tokens: int = Field(1024, ge=1, le=8192)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QuoteRequest(BaseModel):
    skill_path: str
    user_message: str
    model: str = "claude-sonnet-4-6"
    max_tokens: int = Field(1024, ge=1, le=8192)


class InvokeResponse(BaseModel):
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    duration_ms: int
    total_cost_usd: float
    invoice: str
    invocation_id: str


class QuoteResponse(BaseModel):
    estimated_input_tokens: int
    estimated_cost_usd: float
    invoice: str


class SkillInfo(BaseModel):
    name: str
    description: str
    body_chars: int
    resources: list[str]


# ----- Endpoints ------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/skills/{path:path}", response_model=SkillInfo)
def inspect_skill(path: str) -> SkillInfo:
    """Return frontmatter + body length + resources for a skill on disk."""
    try:
        skill = load_skill(path)
    except SkillLoadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return SkillInfo(
        name=skill.name,
        description=skill.description,
        body_chars=len(skill.body),
        resources=[r.name for r in skill.resources],
    )


@app.post("/quote", response_model=QuoteResponse)
def get_quote(req: QuoteRequest) -> QuoteResponse:
    """Estimate the cost of invoking a skill — useful for checkout screens."""
    try:
        skill = load_skill(req.skill_path)
    except SkillLoadError as e:
        raise HTTPException(status_code=400, detail=str(e))
    estimated_input = len(skill.system_prompt_addition()) // 4 + len(req.user_message) // 4
    quote = calculate_price(input_tokens=estimated_input, output_tokens=req.max_tokens)
    return QuoteResponse(
        estimated_input_tokens=estimated_input,
        estimated_cost_usd=quote.total_usd,
        invoice=format_price(quote),
    )


@app.post("/invoke", response_model=InvokeResponse)
def invoke(req: InvokeRequest) -> InvokeResponse:
    """Run a skill — verify payment (via hook), call Claude, record usage."""
    try:
        skill = load_skill(req.skill_path)
    except SkillLoadError as e:
        raise HTTPException(status_code=400, detail=str(e))

    invocation = SkillInvocation(
        skill=skill,
        user_message=req.user_message,
        model=req.model,
        max_tokens=req.max_tokens,
        metadata=req.metadata,
    )
    try:
        result = get_runtime().invoke(invocation)
    except PaymentRequired as e:
        raise HTTPException(status_code=402, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("Skill invocation failed")
        raise HTTPException(status_code=500, detail=f"Invocation failed: {e!r}")

    return InvokeResponse(
        text=result.text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        model=result.model,
        duration_ms=result.duration_ms,
        total_cost_usd=result.quote.total_usd,
        invoice=format_price(result.quote),
        invocation_id=result.invocation_id,
    )


# ----- Optional Stripe webhook example (commented — uncomment + supply keys) -


# import stripe
#
# @app.post("/stripe/webhook")
# async def stripe_webhook(request: Request) -> dict:
#     payload = await request.body()
#     sig = request.headers.get("stripe-signature", "")
#     try:
#         event = stripe.Webhook.construct_event(
#             payload, sig, os.environ["STRIPE_WEBHOOK_SECRET"]
#         )
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=f"Bad signature: {e}")
#     # TODO: dispatch on event["type"], mark buyer as paid in your DB, etc.
#     return {"received": True}