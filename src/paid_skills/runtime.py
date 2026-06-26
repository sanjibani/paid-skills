"""Runtime — load skills, call Anthropic, meter usage, return results.

Designed to be backend-agnostic. The OSS core ships with:
- ``InMemoryMeter`` — default meter, no I/O, great for tests and CLI use
- ``PaymentHook`` protocol — your adapter implements this for Stripe / Lightning / USDC

Persistence / auth / frontends live outside this package.
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import anthropic

from .pricing import PriceQuote, calculate_price
from .skill_loader import Skill, load_skill


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillInvocation:
    """A request to invoke a paid skill.

    Attributes:
        skill: The loaded Skill to execute.
        user_message: The user's request / question.
        model: Anthropic model id (e.g. ``claude-sonnet-4-6``).
        max_tokens: Max output tokens.
        metadata: Free-form dict (user_id, payment_id, session_id, etc).
    """

    skill: Skill
    user_message: str
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillResult:
    """The output of a paid skill invocation.

    Attributes:
        text: Claude's reply (text only — tool calls are out of scope for now).
        input_tokens: Tokens sent (incl. system prompt + skill body + user msg).
        output_tokens: Tokens generated.
        model: Model id actually used.
        duration_ms: Wall-clock time for the call.
        quote: The PriceQuote the buyer was charged.
        invocation_id: UUID for traceability.
    """

    text: str
    input_tokens: int
    output_tokens: int
    model: str
    duration_ms: int
    quote: PriceQuote
    invocation_id: str


@dataclass(frozen=True)
class UsageRecord:
    """One metered invocation — feed this to your billing backend."""

    invocation_id: str
    skill_name: str
    timestamp: str
    input_tokens: int
    output_tokens: int
    total_cost_usd: float
    metadata: dict[str, Any] = field(default_factory=dict)


class PaymentHook(Protocol):
    """Implement this to plug a payment backend (Stripe / Lightning / USDC).

    The runtime calls ``before_invocation`` to verify the buyer has paid (or
    has a valid subscription), and ``after_invocation`` to record the actual
    charge. Raise ``PaymentRequired`` from before_invocation if not paid.
    """

    def before_invocation(self, invocation: SkillInvocation) -> None:
        """Verify the buyer is authorized. Raise ``PaymentRequired`` if not."""

    def after_invocation(self, invocation: SkillInvocation, result: SkillResult) -> None:
        """Record the completed charge. Best-effort; failures should log."""


class PaymentRequired(RuntimeError):
    """Raised by ``PaymentHook.before_invocation`` when payment is missing."""


class Meter(Protocol):
    """Records one ``UsageRecord`` per invocation. Default is in-memory."""

    def record(self, usage: UsageRecord) -> None: ...


class InMemoryMeter:
    """Default Meter — keeps usage in a process-local list. Tests + CLI only."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def record(self, usage: UsageRecord) -> None:
        self.records.append(usage)


class PaidSkillRuntime:
    """The main runtime. Wire up an Anthropic client, optional payment hook, optional meter."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        client: anthropic.Anthropic | None = None,
        meter: Meter | None = None,
        payment_hook: PaymentHook | None = None,
        cost_per_1k_tokens_usd: float = 0.018,
        per_call_usd: float = 0.01,
        token_margin: float = 0.30,
    ) -> None:
        self.client = client or anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )
        if not self.client.api_key:
            raise RuntimeError(
                "Anthropic API key missing. Set ANTHROPIC_API_KEY env var or pass api_key=..."
            )
        self.meter: Meter = meter or InMemoryMeter()
        self.payment_hook = payment_hook
        self.cost_per_1k_tokens_usd = cost_per_1k_tokens_usd
        self.per_call_usd = per_call_usd
        self.token_margin = token_margin

    def invoke(self, invocation: SkillInvocation) -> SkillResult:
        """Load + verify payment + call Claude + meter + return result."""
        if self.payment_hook is not None:
            self.payment_hook.before_invocation(invocation)

        invocation_id = str(uuid.uuid4())
        system_prompt = invocation.skill.system_prompt_addition()

        start = time.monotonic()
        response = self.client.messages.create(
            model=invocation.model,
            max_tokens=invocation.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": invocation.user_message}],
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
        text = "\n".join(text_blocks).strip()
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        quote = calculate_price(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_per_1k_tokens_usd=self.cost_per_1k_tokens_usd,
            per_call_usd=self.per_call_usd,
            margin=self.token_margin,
        )

        result = SkillResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=response.model,
            duration_ms=duration_ms,
            quote=quote,
            invocation_id=invocation_id,
        )

        self.meter.record(
            UsageRecord(
                invocation_id=invocation_id,
                skill_name=invocation.skill.name,
                timestamp=datetime.now(timezone.utc).isoformat(),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_cost_usd=quote.total_usd,
                metadata=invocation.metadata,
            )
        )

        if self.payment_hook is not None:
            try:
                self.payment_hook.after_invocation(invocation, result)
            except Exception as e:  # noqa: BLE001
                logger.warning("Payment hook after_invocation failed: %r", e)

        return result

    def invoke_from_path(
        self,
        skill_path: str | Path,
        user_message: str,
        **kwargs: Any,
    ) -> SkillResult:
        """Convenience: load a skill from disk and invoke it."""
        skill = load_skill(skill_path)
        return self.invoke(
            SkillInvocation(skill=skill, user_message=user_message, **kwargs)
        )