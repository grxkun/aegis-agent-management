"""
Agent Handshake Protocol — Pydantic V2 Schemas.

This module defines the standardised JSON contract that external agents must
follow when communicating with Aegis.  The protocol has three phases:

    1. **Handshake Request** — Agent identifies itself and declares intent.
    2. **Permission Request** — Agent asks for specific resources.
    3. **Permission Response** — Governor returns a grant/rejection with
       metadata the agent can use for retry logic or model selection.

All fields use ``snake_case`` in Python but serialise to ``camelCase`` JSON
via Pydantic's ``alias_generator`` for idiomatic HTTP API consumption.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GrantStatus(str, Enum):
    GRANTED = "GRANTED"
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    ESCALATED = "ESCALATED"


class ResourceType(str, Enum):
    LLM_TOKENS = "llm_tokens"
    GAS_BUDGET = "gas_budget"
    API_CALL = "api_call"
    FILE_LOCK = "file_lock"
    WALLET_LOCK = "wallet_lock"
    COMPUTE = "compute"


# ---------------------------------------------------------------------------
# Handshake — Agent self-identification
# ---------------------------------------------------------------------------

class AgentHandshake(BaseModel):
    """
    Initial handshake payload.  An agent must identify itself once per
    session before issuing permission requests.

    Example JSON::

        {
            "agent_id": "crew-researcher-01",
            "agent_framework": "crewai",
            "capabilities": ["web_search", "code_gen"],
            "max_priority": 7,
            "session_budget_usd": 2.50
        }
    """
    agent_id: str = Field(
        ..., min_length=1, max_length=128,
        description="Unique agent identifier.",
    )
    agent_framework: str = Field(
        default="custom",
        description="Framework powering this agent (crewai, langgraph, custom, etc.).",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Declared capabilities of the agent.",
    )
    max_priority: int = Field(
        default=5, ge=1, le=10,
        description="Maximum priority level this agent may claim.",
    )
    session_budget_usd: Optional[float] = Field(
        default=None, ge=0,
        description="Optional per-session budget cap in USD.",
    )


# ---------------------------------------------------------------------------
# Permission Request
# ---------------------------------------------------------------------------

class PermissionRequest(BaseModel):
    """
    Request from an agent to consume a governed resource.

    Example JSON::

        {
            "agent_id": "crew-researcher-01",
            "task_intent": "Summarise the latest Sui governance proposals",
            "resource_type": "llm_tokens",
            "estimated_cost_usd": 0.035,
            "priority": 5,
            "logic_density": 3,
            "target_resource": null,
            "metadata": {"source_url": "https://forums.sui.io"}
        }
    """
    agent_id: str = Field(..., min_length=1, max_length=128)
    task_intent: str = Field(
        ..., min_length=1, max_length=1024,
        description="Human-readable description of the intended action.",
    )
    resource_type: ResourceType = Field(
        default=ResourceType.LLM_TOKENS,
        description="Category of resource being requested.",
    )
    estimated_cost_usd: float = Field(
        ..., ge=0,
        description="Projected cost in USD.",
    )
    priority: int = Field(
        default=5, ge=1, le=10,
        description="Urgency / importance of this request.",
    )
    logic_density: int = Field(
        default=5, ge=1, le=10,
        description="Complexity score for model routing (1=trivial, 10=expert).",
    )
    target_resource: Optional[str] = Field(
        default=None,
        description="Specific resource to lock (e.g. 'wallet_sui_main').",
    )
    confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="Agent's self-assessed confidence (0.0–1.0). Below 0.70 triggers HITL.",
    )
    metadata: dict = Field(
        default_factory=dict,
        description="Arbitrary key-value pairs for audit / context.",
    )

    @model_validator(mode="after")
    def _lock_resources_need_target(self) -> "PermissionRequest":
        lock_types = {ResourceType.FILE_LOCK, ResourceType.WALLET_LOCK}
        if self.resource_type in lock_types and not self.target_resource:
            raise ValueError(
                f"resource_type '{self.resource_type.value}' requires a "
                f"'target_resource' to be specified."
            )
        return self


# ---------------------------------------------------------------------------
# Permission Response
# ---------------------------------------------------------------------------

class PermissionResponse(BaseModel):
    """
    Governor's response to a ``PermissionRequest``.

    Example JSON::

        {
            "status": "GRANTED",
            "agent_id": "crew-researcher-01",
            "reason": "Budget available and policy satisfied.",
            "remaining_budget_usd": 9.965,
            "recommended_model": "gemini-flash",
            "lock_token": null,
            "retry_after_seconds": null,
            "granted_at": "2026-03-29T12:00:00Z"
        }
    """
    status: GrantStatus
    agent_id: str
    reason: str
    remaining_budget_usd: float = Field(ge=0)
    recommended_model: Optional[str] = Field(
        default=None,
        description="Model alias suggested by the Arbiter.",
    )
    lock_token: Optional[str] = Field(
        default=None,
        description="Opaque token for lock-type grants (use to release).",
    )
    escalation_id: Optional[str] = Field(
        default=None,
        description="HITL escalation ID when the request required human approval.",
    )
    retry_after_seconds: Optional[int] = Field(
        default=None,
        description="Hint for the agent to retry after N seconds (PENDING status).",
    )
    granted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ---------------------------------------------------------------------------
# Lock Release
# ---------------------------------------------------------------------------

class LockReleaseRequest(BaseModel):
    """Payload to explicitly release a previously acquired lock."""
    agent_id: str = Field(..., min_length=1, max_length=128)
    resource: str = Field(..., description="Name of the locked resource.")
    lock_token: str = Field(..., description="Token received in the grant.")


class LockReleaseResponse(BaseModel):
    success: bool
    message: str


# ---------------------------------------------------------------------------
# Health / Status
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    redis_connected: bool
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
