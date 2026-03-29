"""
LLM Adapter — Token Usage Tracking for Major AI Providers.

Tracks per-agent, per-model token consumption so the ResourceGovernor can
make informed budget decisions.  Designed to wrap the raw provider SDKs
rather than replace them — agents still call OpenAI/Anthropic/Google
directly, then report usage through this adapter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import redis.asyncio as redis

logger = logging.getLogger("aegis.adapters.llm")


# Cost per 1K tokens (input/output) — updated as of 2025-Q4.
# Operators should override via config for current pricing.
PROVIDER_PRICING: dict[str, dict[str, float]] = {
    # alias -> {"input": $/1k, "output": $/1k}
    "gpt-4o":         {"input": 0.0025,  "output": 0.0100},
    "gpt-4o-mini":    {"input": 0.00015, "output": 0.00060},
    "o3":             {"input": 0.0100,  "output": 0.0400},
    "claude-opus":    {"input": 0.0150,  "output": 0.0750},
    "claude-sonnet":  {"input": 0.0030,  "output": 0.0150},
    "claude-haiku":   {"input": 0.00025, "output": 0.00125},
    "gemini-flash":   {"input": 0.00010, "output": 0.00040},
    "gemini-pro":     {"input": 0.00125, "output": 0.00500},
}


@dataclass(frozen=True)
class UsageRecord:
    """Immutable record of a single LLM call's token consumption."""
    agent_id: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class LLMAdapter:
    """
    Tracks and budgets LLM token usage across all agents.

    Parameters
    ----------
    redis_url : str
        Redis connection string for persistent usage counters.
    pricing : dict
        Override pricing table.
    """

    redis_url: str = "redis://localhost:6379/0"
    pricing: dict[str, dict[str, float]] = field(
        default_factory=lambda: dict(PROVIDER_PRICING)
    )
    _redis: redis.Redis = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._redis = redis.from_url(self.redis_url, decode_responses=True)

    def estimate_cost(
        self, model: str, input_tokens: int, output_tokens: int
    ) -> float:
        """
        Estimate cost in USD for a planned LLM call.

        Returns 0.0 if the model is not in the pricing table (log warning).
        """
        rates = self.pricing.get(model)
        if not rates:
            logger.warning("No pricing data for model '%s' — returning $0.", model)
            return 0.0
        return (
            (input_tokens / 1000) * rates["input"]
            + (output_tokens / 1000) * rates["output"]
        )

    async def record_usage(
        self,
        agent_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> UsageRecord:
        """
        Record token usage after an LLM call completes.

        Persists:
            - Per-agent daily total cost in Redis.
            - Per-model daily token counts for analytics.
        """
        cost = self.estimate_cost(model, input_tokens, output_tokens)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Aggregate cost for the Governor's budget checks.
        cost_key = f"aegis:llm:cost:{agent_id}:{today}"
        await self._redis.incrbyfloat(cost_key, cost)
        await self._redis.expire(cost_key, 48 * 3600)

        # Per-model token counters for observability.
        token_key = f"aegis:llm:tokens:{model}:{today}"
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.hincrby(token_key, "input", input_tokens)
            pipe.hincrby(token_key, "output", output_tokens)
            pipe.expire(token_key, 48 * 3600)
            await pipe.execute()

        record = UsageRecord(
            agent_id=agent_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )
        logger.info(
            "LLM usage recorded: agent=%s model=%s in=%d out=%d cost=$%.6f",
            agent_id, model, input_tokens, output_tokens, cost,
        )
        return record

    async def get_daily_cost(self, agent_id: str) -> float:
        """Return total LLM spend for an agent today."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw = await self._redis.get(f"aegis:llm:cost:{agent_id}:{today}")
        return float(raw) if raw else 0.0

    async def close(self) -> None:
        await self._redis.aclose()
