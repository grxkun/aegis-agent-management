"""
Human-in-the-Loop (HITL) Module — Escalation Gateway.

Intercepts high-risk or high-cost agent requests and pauses execution until
a human operator provides explicit approval or rejection.

Escalation Triggers (any one is sufficient):
    1. Estimated cost > configurable threshold (default $5.00).
    2. Task intent matches a sensitive-action pattern (withdraw_funds,
       delete_resource, change_admin_policy).
    3. Agent-declared confidence < 0.70.

Concurrency Model:
    Each escalated request gets its own ``asyncio.Event`` keyed by a unique
    ``approval_id``.  The requesting coroutine awaits that event with a
    timeout.  Multiple agents can be waiting for approval simultaneously
    without blocking each other or the event loop.

State Persistence:
    Approval state is mirrored in Redis so that the Telegram webhook (which
    may arrive on a different process or after a restart) can resolve the
    correct pending request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import redis.asyncio as redis
from pydantic import BaseModel, Field

logger = logging.getLogger("aegis.hitl")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    TIMED_OUT = "TIMED_OUT"


class EscalationRequest(BaseModel):
    """Payload sent to the human operator for review."""
    approval_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    agent_id: str
    task_intent: str
    estimated_cost_usd: float
    priority: int
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    risk_level: RiskLevel
    escalation_reasons: list[str]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EscalationResult(BaseModel):
    """Final resolution of a HITL review."""
    approval_id: str
    status: ApprovalStatus
    decided_by: Optional[str] = None  # Telegram username or "system:timeout"
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Risk assessment configuration
# ---------------------------------------------------------------------------

# Patterns that always trigger escalation regardless of cost.
_SENSITIVE_INTENT_PATTERNS: list[str] = [
    r"withdraw[_\s]?funds",
    r"delete[_\s]?resource",
    r"change[_\s]?admin[_\s]?policy",
    r"drain[_\s]?wallet",
    r"revoke[_\s]?access",
    r"modify[_\s]?permissions",
]


@dataclass
class HITLConfig:
    """Tuneable thresholds for the HITL escalation gate."""
    cost_threshold_usd: float = 5.0
    confidence_floor: float = 0.70
    approval_timeout_seconds: float = 600.0  # 10 minutes
    sensitive_patterns: list[str] = field(
        default_factory=lambda: list(_SENSITIVE_INTENT_PATTERNS)
    )


# ---------------------------------------------------------------------------
# HITL Manager
# ---------------------------------------------------------------------------

class HITLManager:
    """
    Manages the full lifecycle of human-in-the-loop escalations.

    Responsibilities:
        1. **Assess risk** — decide if a request must be escalated.
        2. **Park the coroutine** — create an ``asyncio.Event`` and wait.
        3. **Persist state** — mirror pending approvals in Redis for the
           Telegram webhook to resolve.
        4. **Resume or reject** — when the human responds (or timeout fires).

    Parameters
    ----------
    redis_client : redis.Redis
        Shared async Redis connection (same instance as the Governor).
    config : HITLConfig
        Escalation thresholds.
    notifier : callable | None
        Async callback ``(EscalationRequest) -> None`` that sends the
        approval prompt to the operator (e.g. Telegram).  If None,
        escalations are logged but no notification is sent.
    """

    _KEY_PREFIX = "aegis:hitl:"

    def __init__(
        self,
        redis_client: redis.Redis,
        config: Optional[HITLConfig] = None,
        notifier=None,
    ) -> None:
        self._redis = redis_client
        self._config = config or HITLConfig()
        self._notifier = notifier

        # In-process waiters: approval_id -> asyncio.Event
        self._events: dict[str, asyncio.Event] = {}
        # Cached results so the waking coroutine can read the outcome.
        self._results: dict[str, EscalationResult] = {}

    # ------------------------------------------------------------------
    # Risk Assessment
    # ------------------------------------------------------------------

    def assess_risk(
        self,
        task_intent: str,
        estimated_cost: float,
        confidence: float = 1.0,
    ) -> tuple[RiskLevel, list[str]]:
        """
        Evaluate whether a request must be escalated to a human.

        Returns
        -------
        (RiskLevel, reasons)
            ``RiskLevel.HIGH`` means the request **must** be escalated.
        """
        reasons: list[str] = []

        # ── Cost gate ────────────────────────────────────────────────
        if estimated_cost > self._config.cost_threshold_usd:
            reasons.append(
                f"Estimated cost ${estimated_cost:.2f} exceeds "
                f"HITL threshold ${self._config.cost_threshold_usd:.2f}"
            )

        # ── Sensitive intent gate ────────────────────────────────────
        for pattern in self._config.sensitive_patterns:
            if re.search(pattern, task_intent, re.IGNORECASE):
                reasons.append(f"Intent matches sensitive pattern: '{pattern}'")
                break  # One match is enough.

        # ── Confidence gate ──────────────────────────────────────────
        if confidence < self._config.confidence_floor:
            reasons.append(
                f"Agent confidence {confidence:.2f} is below "
                f"floor {self._config.confidence_floor:.2f}"
            )

        if reasons:
            return RiskLevel.HIGH, reasons

        if estimated_cost > self._config.cost_threshold_usd * 0.5:
            return RiskLevel.MEDIUM, []

        return RiskLevel.LOW, []

    # ------------------------------------------------------------------
    # Escalation lifecycle
    # ------------------------------------------------------------------

    async def escalate_and_wait(
        self,
        agent_id: str,
        task_intent: str,
        estimated_cost: float,
        priority: int,
        confidence: float = 1.0,
        escalation_reasons: Optional[list[str]] = None,
    ) -> EscalationResult:
        """
        Escalate a request to a human and **block** until resolved or timed out.

        This is the primary integration point.  The calling coroutine will
        suspend here without blocking the event loop — other agents and HTTP
        requests continue to be served.

        Returns
        -------
        EscalationResult
            With status APPROVED, REJECTED, or TIMED_OUT.
        """
        esc = EscalationRequest(
            agent_id=agent_id,
            task_intent=task_intent,
            estimated_cost_usd=estimated_cost,
            priority=priority,
            confidence=confidence,
            risk_level=RiskLevel.HIGH,
            escalation_reasons=escalation_reasons or [],
        )

        # ── Persist to Redis ─────────────────────────────────────────
        await self._persist_pending(esc)

        # ── Create in-process waiter ─────────────────────────────────
        event = asyncio.Event()
        self._events[esc.approval_id] = event

        # ── Notify operator ──────────────────────────────────────────
        if self._notifier:
            try:
                await self._notifier(esc)
            except Exception as exc:
                logger.error("HITL notifier failed for %s: %s", esc.approval_id, exc)
        else:
            logger.warning(
                "HITL escalation %s has no notifier configured — "
                "resolve via API or it will time out.",
                esc.approval_id,
            )

        logger.info(
            "HITL ESCALATED approval_id=%s agent=%s cost=$%.2f reasons=%s",
            esc.approval_id, agent_id, estimated_cost, escalation_reasons,
        )

        # ── Async wait with timeout ──────────────────────────────────
        try:
            await asyncio.wait_for(
                event.wait(),
                timeout=self._config.approval_timeout_seconds,
            )
        except asyncio.TimeoutError:
            result = EscalationResult(
                approval_id=esc.approval_id,
                status=ApprovalStatus.TIMED_OUT,
                decided_by="system:timeout",
            )
            self._results[esc.approval_id] = result
            await self._persist_result(result)
            logger.warning(
                "HITL TIMED OUT approval_id=%s agent=%s after %.0fs",
                esc.approval_id, agent_id,
                self._config.approval_timeout_seconds,
            )
        finally:
            self._events.pop(esc.approval_id, None)

        return self._results.pop(esc.approval_id, EscalationResult(
            approval_id=esc.approval_id,
            status=ApprovalStatus.TIMED_OUT,
            decided_by="system:timeout",
        ))

    async def resolve(
        self,
        approval_id: str,
        approved: bool,
        decided_by: str = "operator",
    ) -> bool:
        """
        Called by the Telegram webhook (or admin API) to approve/reject.

        Returns True if a waiting coroutine was found and woken, False
        if the approval_id is unknown (already timed out or invalid).
        """
        status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        result = EscalationResult(
            approval_id=approval_id,
            status=status,
            decided_by=decided_by,
        )

        # Persist to Redis first (survives process restarts).
        await self._persist_result(result)

        # Wake the in-process waiter if it exists.
        event = self._events.get(approval_id)
        if event is None:
            logger.warning(
                "HITL resolve called for unknown/expired approval_id=%s",
                approval_id,
            )
            return False

        self._results[approval_id] = result
        event.set()
        logger.info(
            "HITL RESOLVED approval_id=%s status=%s by=%s",
            approval_id, status.value, decided_by,
        )
        return True

    # ------------------------------------------------------------------
    # Pending approvals query
    # ------------------------------------------------------------------

    async def get_pending(self) -> list[dict]:
        """Return all currently pending escalations from Redis."""
        keys = []
        async for key in self._redis.scan_iter(f"{self._KEY_PREFIX}pending:*"):
            keys.append(key)

        pending = []
        for key in keys:
            raw = await self._redis.get(key)
            if raw:
                import json
                pending.append(json.loads(raw))
        return pending

    # ------------------------------------------------------------------
    # Redis persistence
    # ------------------------------------------------------------------

    async def _persist_pending(self, esc: EscalationRequest) -> None:
        key = f"{self._KEY_PREFIX}pending:{esc.approval_id}"
        ttl = int(self._config.approval_timeout_seconds) + 60  # grace
        await self._redis.set(key, esc.model_dump_json(), ex=ttl)

    async def _persist_result(self, result: EscalationResult) -> None:
        # Remove from pending.
        pending_key = f"{self._KEY_PREFIX}pending:{result.approval_id}"
        await self._redis.delete(pending_key)

        # Store result for audit (24h TTL).
        result_key = f"{self._KEY_PREFIX}result:{result.approval_id}"
        await self._redis.set(result_key, result.model_dump_json(), ex=86400)
