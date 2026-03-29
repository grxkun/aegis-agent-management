"""
Aegis API Routes — The Governor's HTTP Surface.

All agent interaction flows through these endpoints:

    POST /handshake          — Agent registers with the Governor.
    POST /request-permission — Agent requests resource access (with HITL gate).
    POST /release-lock       — Agent releases a held lock.
    GET  /budget/{agent_id}  — Query remaining budget.
    GET  /health             — Liveness / readiness probe.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from aegisimport __version__
from aegis.api.schemas import (
    AgentHandshake,
    GrantStatus,
    HealthResponse,
    LockReleaseRequest,
    LockReleaseResponse,
    PermissionRequest,
    PermissionResponse,
    ResourceType,
)
from aegis.core.arbiter import ModelArbiter
from aegis.core.governor import ResourceGovernor
from aegis.core.hitl import ApprovalStatus, HITLManager, RiskLevel
from aegis.core.state_lock import StateLockManager

logger = logging.getLogger("aegis.api")

router = APIRouter()

# ---------------------------------------------------------------------------
# In-memory session registry (production would use Redis).
# ---------------------------------------------------------------------------
_registered_agents: dict[str, AgentHandshake] = {}


# ---------------------------------------------------------------------------
# Dependency helpers — components are attached to app.state in main.py.
# ---------------------------------------------------------------------------

def _governor(request: Request) -> ResourceGovernor:
    return request.app.state.governor


def _arbiter(request: Request) -> ModelArbiter:
    return request.app.state.arbiter


def _lock_mgr(request: Request) -> StateLockManager:
    return request.app.state.lock_manager


def _hitl(request: Request) -> HITLManager:
    return request.app.state.hitl_manager


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/handshake", response_model=dict, tags=["lifecycle"])
async def agent_handshake(body: AgentHandshake):
    """
    Register an agent with the Governor.

    Agents must handshake once per session before requesting resources.
    Re-handshaking updates the registration.
    """
    _registered_agents[body.agent_id] = body
    logger.info("Agent registered: %s (framework=%s)", body.agent_id, body.agent_framework)
    return {
        "status": "registered",
        "agent_id": body.agent_id,
        "message": f"Welcome, {body.agent_id}. You may now request resources.",
    }


@router.post("/request-permission", response_model=PermissionResponse, tags=["governance"])
async def request_permission(body: PermissionRequest, request: Request):
    """
    Primary governance endpoint with Human-in-the-Loop gate.

    Sequence:
        1. Verify the agent has handshaked.
        2. Run the ModelArbiter to get a model recommendation.
        3. **HITL risk assessment** — escalate if HIGH risk.
        4. Ask the ResourceGovernor for budget approval.
        5. If a lock-type resource, acquire the lock.
        6. Return a unified PermissionResponse.

    If the request is escalated to HITL, this endpoint **blocks** (without
    tying up the event loop) until the human responds or the 10-minute
    timeout expires.
    """
    # ── Gate: registration check ─────────────────────────────────
    if body.agent_id not in _registered_agents:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{body.agent_id}' has not completed handshake.",
        )

    governor = _governor(request)
    arbiter = _arbiter(request)
    lock_mgr = _lock_mgr(request)
    hitl = _hitl(request)

    # ── Model recommendation ─────────────────────────────────────
    recommended_model: Optional[str] = None
    if body.resource_type == ResourceType.LLM_TOKENS:
        spec = arbiter.recommend(body.logic_density)
        recommended_model = spec.alias if spec else None

    # ── HITL risk assessment ─────────────────────────────────────
    risk_level, escalation_reasons = hitl.assess_risk(
        task_intent=body.task_intent,
        estimated_cost=body.estimated_cost_usd,
        confidence=body.confidence,
    )

    if risk_level == RiskLevel.HIGH:
        logger.warning(
            "HITL ESCALATION agent=%s intent='%s' cost=$%.2f reasons=%s",
            body.agent_id, body.task_intent, body.estimated_cost_usd,
            escalation_reasons,
        )
        result = await hitl.escalate_and_wait(
            agent_id=body.agent_id,
            task_intent=body.task_intent,
            estimated_cost=body.estimated_cost_usd,
            priority=body.priority,
            confidence=body.confidence,
            escalation_reasons=escalation_reasons,
        )

        if result.status == ApprovalStatus.TIMED_OUT:
            return PermissionResponse(
                status=GrantStatus.REJECTED,
                agent_id=body.agent_id,
                reason=f"HITL approval timed out after {hitl._config.approval_timeout_seconds:.0f}s. "
                       f"Request auto-rejected for safety.",
                remaining_budget_usd=0.0,
                recommended_model=recommended_model,
                escalation_id=result.approval_id,
            )

        if result.status == ApprovalStatus.REJECTED:
            return PermissionResponse(
                status=GrantStatus.REJECTED,
                agent_id=body.agent_id,
                reason=f"HITL rejected by {result.decided_by}.",
                remaining_budget_usd=0.0,
                recommended_model=recommended_model,
                escalation_id=result.approval_id,
            )

        logger.info(
            "HITL APPROVED approval_id=%s by=%s — proceeding to budget check.",
            result.approval_id, result.decided_by,
        )

    # ── Budget check via Governor ────────────────────────────────
    grant = await governor.request_permission(
        agent_id=body.agent_id,
        task_intent=body.task_intent,
        estimated_cost=body.estimated_cost_usd,
        priority=body.priority,
    )

    if grant.status.value != "GRANTED":
        return PermissionResponse(
            status=GrantStatus(grant.status.value),
            agent_id=body.agent_id,
            reason=grant.reason,
            remaining_budget_usd=grant.remaining_budget,
            recommended_model=recommended_model,
        )

    # ── Lock acquisition (if applicable) ─────────────────────────
    lock_token: Optional[str] = None
    if body.resource_type in {ResourceType.WALLET_LOCK, ResourceType.FILE_LOCK}:
        try:
            async with lock_mgr.acquire(
                resource=body.target_resource,
                agent_id=body.agent_id,
                priority=body.priority,
                timeout=5.0,
            ) as lock_info:
                lock_token = lock_info.token
        except Exception as exc:
            return PermissionResponse(
                status=GrantStatus.REJECTED,
                agent_id=body.agent_id,
                reason=f"Could not acquire lock on '{body.target_resource}': {exc}",
                remaining_budget_usd=grant.remaining_budget,
                recommended_model=recommended_model,
            )

    return PermissionResponse(
        status=GrantStatus.GRANTED,
        agent_id=body.agent_id,
        reason=grant.reason,
        remaining_budget_usd=grant.remaining_budget,
        recommended_model=recommended_model,
        lock_token=lock_token,
    )


@router.post("/release-lock", response_model=LockReleaseResponse, tags=["governance"])
async def release_lock(body: LockReleaseRequest, request: Request):
    """Release a resource lock previously acquired via /request-permission."""
    lock_mgr = _lock_mgr(request)
    released = await lock_mgr.force_release(body.resource)
    if released:
        return LockReleaseResponse(success=True, message=f"Lock on '{body.resource}' released.")
    return LockReleaseResponse(success=False, message=f"No active lock found on '{body.resource}'.")


@router.get("/budget/{agent_id}", tags=["observability"])
async def get_budget(agent_id: str, request: Request):
    """Return the remaining daily budget for a specific agent."""
    governor = _governor(request)
    remaining = await governor.get_remaining_budget(agent_id)
    return {"agent_id": agent_id, "remaining_budget_usd": remaining}


@router.get("/health", response_model=HealthResponse, tags=["observability"])
async def health_check(request: Request):
    """Liveness probe — confirms the Governor and Redis are operational."""
    governor = _governor(request)
    redis_ok = True
    try:
        await governor._redis.ping()
    except Exception:
        redis_ok = False

    return HealthResponse(
        status="ok" if redis_ok else "degraded",
        version=__version__,
        redis_connected=redis_ok,
    )
