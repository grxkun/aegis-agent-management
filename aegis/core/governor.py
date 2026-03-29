"""
ResourceGovernor — The Central Control Plane of Aegis.

The Governor is the single authority that agents must consult before consuming
any resource (LLM tokens, gas, API calls, compute).  It enforces:

  1. **Day-Limits**: Per-agent daily budget ceilings stored in Redis.
  2. **Policy Validation**: Every request is checked against the PolicyEngine
     before a grant is issued.
  3. **Audit Trail**: Every decision (GRANTED / PENDING / REJECTED) is logged
     with the reasoning so operators can reconstruct behaviour post-hoc.

Design Principle — Zero-Waste (Muda):
    No agent should consume a high-tier resource for a low-tier task.  The
    Governor delegates model selection to the ModelArbiter, ensuring cost
    efficiency is enforced at the infrastructure layer, not left to the agent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import redis.asyncio as redis
from pydantic import BaseModel, Field

from aegis.policy.engine import PolicyEngine, PolicyVerdict

logger = logging.getLogger("aegis.governor")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class GrantStatus(str, Enum):
    """Possible outcomes of a resource-permission request."""
    GRANTED = "GRANTED"
    PENDING = "PENDING"
    REJECTED = "REJECTED"


class GrantResult(BaseModel):
    """Immutable record returned to the requesting agent."""
    status: GrantStatus
    agent_id: str
    reason: str
    remaining_budget: float = Field(
        ge=0,
        description="How much of the agent's daily budget remains after this grant.",
    )
    recommended_model: Optional[str] = Field(
        default=None,
        description="Model alias recommended by the Arbiter, if applicable.",
    )
    granted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Governor
# ---------------------------------------------------------------------------

class ResourceGovernor:
    """
    Central authority for resource allocation across all managed agents.

    Parameters
    ----------
    redis_url : str
        Redis connection string (e.g. ``redis://localhost:6379/0``).
    default_day_limit : float
        Fallback daily budget (USD) when an agent has no explicit limit.
    policy_engine : PolicyEngine | None
        Pluggable policy validator.  A default engine is created if omitted.
    """

    # Redis key templates
    _KEY_SPENT = "aegis:budget:{agent_id}:spent:{date}"
    _KEY_LIMIT = "aegis:budget:{agent_id}:day_limit"

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        default_day_limit: float = 10.0,
        policy_engine: Optional[PolicyEngine] = None,
    ) -> None:
        self._redis: redis.Redis = redis.from_url(
            redis_url, decode_responses=True
        )
        self._default_day_limit = default_day_limit
        self._policy = policy_engine or PolicyEngine()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def request_permission(
        self,
        agent_id: str,
        task_intent: str,
        estimated_cost: float,
        priority: int = 5,
    ) -> GrantResult:
        """
        The primary entry-point for agents requesting permission to execute.

        Sequence:
            1. Validate against the PolicyEngine.
            2. Check Redis-backed day-limit for the agent.
            3. Return a ``GrantResult`` with status and reasoning.

        Parameters
        ----------
        agent_id : str
            Unique identifier for the requesting agent.
        task_intent : str
            Free-text description of what the agent intends to do.
        estimated_cost : float
            Projected cost in USD for this action.
        priority : int
            1 (lowest) — 10 (highest).  Higher priority may preempt locks.

        Returns
        -------
        GrantResult
        """
        # ── Step 1: Policy gate ──────────────────────────────────────
        try:
            verdict = self._policy.evaluate(
                agent_id=agent_id,
                task_intent=task_intent,
                estimated_cost=estimated_cost,
                priority=priority,
            )
        except Exception as exc:
            logger.error("Policy engine error for agent=%s: %s", agent_id, exc)
            return self._reject(agent_id, f"Policy engine unavailable: {exc}")

        if verdict.status == "DENIED":
            return self._reject(agent_id, f"Policy denied: {verdict.reason}")

        # ── Step 2: Budget gate ──────────────────────────────────────
        try:
            remaining = await self._check_budget(agent_id, estimated_cost)
        except redis.RedisError as exc:
            logger.error("Redis error during budget check: %s", exc)
            return self._reject(agent_id, f"Budget service unavailable: {exc}")

        if remaining is None:
            return self._reject(
                agent_id,
                f"Daily budget exceeded. Estimated cost ${estimated_cost:.4f} "
                f"would breach the day-limit.",
            )

        # ── Step 3: Commit spend and grant ───────────────────────────
        try:
            await self._commit_spend(agent_id, estimated_cost)
        except redis.RedisError as exc:
            logger.error("Redis error committing spend: %s", exc)
            return self._reject(agent_id, f"Could not record spend: {exc}")

        logger.info(
            "GRANTED agent=%s cost=%.4f remaining=%.4f intent=%s",
            agent_id, estimated_cost, remaining, task_intent,
        )
        return GrantResult(
            status=GrantStatus.GRANTED,
            agent_id=agent_id,
            reason="Budget available and policy satisfied.",
            remaining_budget=remaining,
        )

    async def set_day_limit(self, agent_id: str, limit: float) -> None:
        """Override the default daily budget for a specific agent."""
        key = self._KEY_LIMIT.format(agent_id=agent_id)
        await self._redis.set(key, str(limit))

    async def get_remaining_budget(self, agent_id: str) -> float:
        """Return how much budget the agent has left today."""
        day_limit = await self._get_day_limit(agent_id)
        spent = await self._get_spent_today(agent_id)
        return max(day_limit - spent, 0.0)

    async def close(self) -> None:
        """Cleanly shut down the Redis connection pool."""
        await self._redis.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _get_day_limit(self, agent_id: str) -> float:
        key = self._KEY_LIMIT.format(agent_id=agent_id)
        raw = await self._redis.get(key)
        if raw is not None:
            return float(raw)
        return self._default_day_limit

    async def _get_spent_today(self, agent_id: str) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = self._KEY_SPENT.format(agent_id=agent_id, date=today)
        raw = await self._redis.get(key)
        return float(raw) if raw else 0.0

    async def _check_budget(
        self, agent_id: str, estimated_cost: float
    ) -> Optional[float]:
        """Return remaining budget *after* the proposed spend, or None if over."""
        day_limit = await self._get_day_limit(agent_id)
        spent = await self._get_spent_today(agent_id)
        remaining = day_limit - spent - estimated_cost
        if remaining < 0:
            return None
        return remaining

    async def _commit_spend(self, agent_id: str, amount: float) -> None:
        """Atomically increment today's spend counter with a 48-hour TTL."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = self._KEY_SPENT.format(agent_id=agent_id, date=today)
        await self._redis.incrbyfloat(key, amount)
        # TTL ensures stale keys self-clean (48h covers timezone edge cases).
        await self._redis.expire(key, 48 * 3600)

    @staticmethod
    def _reject(agent_id: str, reason: str) -> GrantResult:
        return GrantResult(
            status=GrantStatus.REJECTED,
            agent_id=agent_id,
            reason=reason,
            remaining_budget=0.0,
        )
