"""
Webhook Endpoints — Telegram Bot, HITL Administration, & Kaizen API.

Provides:
    POST /telegram/webhook     — Receives updates from Telegram's webhook push.
    POST /hitl/resolve         — Manual approve/reject via HTTP (no Telegram needed).
    GET  /hitl/pending         — List all pending escalations.
    POST /kaizen/experiment    — Start a shadow-mode experiment.
    POST /kaizen/observe       — Record an experiment observation.
    POST /kaizen/evaluate      — Evaluate and decide on an experiment.
    GET  /kaizen/summary       — Get the 24-hour Kaizen summary.
    POST /kaizen/commit        — Commit staged changes.
    POST /kaizen/rollback      — Rollback staged changes.
    GET  /kaizen/evolution-log — View the SCAMPER evolution log.
    GET  /kaizen/muda          — Run the Muda waste detector.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aegis.core.hitl import ApprovalStatus

logger = logging.getLogger("aegis.api.webhook")

webhook_router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class HITLResolveRequest(BaseModel):
    """Manual resolution of an escalation via the admin API."""
    approval_id: str = Field(..., min_length=1)
    approved: bool
    decided_by: str = Field(default="admin_api")


class HITLResolveResponse(BaseModel):
    success: bool
    approval_id: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# Telegram webhook
# ---------------------------------------------------------------------------

@webhook_router.post("/telegram/webhook", tags=["hitl"])
async def telegram_webhook(request: Request):
    """
    Receive Telegram updates and forward them to the approval bot.

    Telegram sends JSON payloads here when the operator interacts with
    inline keyboard buttons.  The bot's dispatcher routes the callback
    to ``HITLManager.resolve()``.
    """
    bot = getattr(request.app.state, "telegram_bot", None)
    if bot is None:
        raise HTTPException(
            status_code=503,
            detail="Telegram bot is not configured.",
        )

    payload = await request.json()
    await bot.process_update(payload)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin API for HITL
# ---------------------------------------------------------------------------

@webhook_router.post(
    "/hitl/resolve",
    response_model=HITLResolveResponse,
    tags=["hitl"],
)
async def resolve_escalation(body: HITLResolveRequest, request: Request):
    """
    Approve or reject a pending escalation via HTTP.

    This is the non-Telegram path — useful for dashboards, CLI tools,
    or integration tests.
    """
    hitl = getattr(request.app.state, "hitl_manager", None)
    if hitl is None:
        raise HTTPException(status_code=503, detail="HITL manager is not initialised.")

    resolved = await hitl.resolve(
        approval_id=body.approval_id,
        approved=body.approved,
        decided_by=body.decided_by,
    )

    if resolved:
        status = "APPROVED" if body.approved else "REJECTED"
        return HITLResolveResponse(
            success=True,
            approval_id=body.approval_id,
            status=status,
            message=f"Escalation {body.approval_id} has been {status}.",
        )

    return HITLResolveResponse(
        success=False,
        approval_id=body.approval_id,
        status="NOT_FOUND",
        message="Escalation not found — it may have already timed out or been resolved.",
    )


@webhook_router.get("/hitl/pending", tags=["hitl"])
async def list_pending(request: Request):
    """Return all pending HITL escalations awaiting human approval."""
    hitl = getattr(request.app.state, "hitl_manager", None)
    if hitl is None:
        raise HTTPException(status_code=503, detail="HITL manager is not initialised.")

    pending = await hitl.get_pending()
    return {"count": len(pending), "pending": pending}


# ---------------------------------------------------------------------------
# Kaizen / SCAMPER API
# ---------------------------------------------------------------------------

class ExperimentStartRequest(BaseModel):
    control_model: str
    experiment_model: str
    logic_density: int = Field(ge=1, le=10)
    description: str = ""


class ObservationRequest(BaseModel):
    experiment_id: str
    is_control: bool
    quality_score: float = Field(ge=0.0, le=1.0)
    cost_usd: float = Field(ge=0.0)
    latency_ms: float = 0.0


@webhook_router.post("/kaizen/experiment", tags=["kaizen"])
async def start_experiment(body: ExperimentStartRequest, request: Request):
    """Start a shadow-mode experiment comparing two models."""
    kaizen = getattr(request.app.state, "kaizen_engine", None)
    if kaizen is None:
        raise HTTPException(status_code=503, detail="Kaizen engine not initialised.")

    exp_id = await kaizen.start_experiment(
        control_model=body.control_model,
        experiment_model=body.experiment_model,
        logic_density=body.logic_density,
        description=body.description,
    )
    return {"experiment_id": exp_id, "status": "RUNNING"}


@webhook_router.post("/kaizen/observe", tags=["kaizen"])
async def record_observation(body: ObservationRequest, request: Request):
    """Record a control or experiment observation."""
    kaizen = getattr(request.app.state, "kaizen_engine", None)
    if kaizen is None:
        raise HTTPException(status_code=503, detail="Kaizen engine not initialised.")

    await kaizen.record_observation(
        experiment_id=body.experiment_id,
        is_control=body.is_control,
        quality_score=body.quality_score,
        cost_usd=body.cost_usd,
        latency_ms=body.latency_ms,
    )
    return {"status": "recorded", "experiment_id": body.experiment_id}


@webhook_router.post("/kaizen/evaluate", tags=["kaizen"])
async def evaluate_experiment(request: Request, experiment_id: str):
    """Evaluate an experiment and determine if it should be promoted."""
    kaizen = getattr(request.app.state, "kaizen_engine", None)
    if kaizen is None:
        raise HTTPException(status_code=503, detail="Kaizen engine not initialised.")

    result = await kaizen.evaluate_experiment(experiment_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Experiment not found or insufficient data.")

    return {
        "experiment_id": result.experiment_id,
        "status": result.status.value,
        "control_model": result.control_model,
        "experiment_model": result.experiment_model,
        "quality_retention_pct": result.quality_retention_pct,
        "cost_savings_pct": result.cost_savings_pct,
    }


@webhook_router.get("/kaizen/summary", tags=["kaizen"])
async def kaizen_summary(request: Request):
    """Get the 24-hour Kaizen summary (same content sent to Telegram)."""
    kaizen = getattr(request.app.state, "kaizen_engine", None)
    if kaizen is None:
        raise HTTPException(status_code=503, detail="Kaizen engine not initialised.")

    summary = await kaizen.generate_kaizen_summary()
    telegram_text = kaizen.format_telegram_summary(summary)
    return {"summary": summary, "telegram_preview": telegram_text}


@webhook_router.post("/kaizen/commit", tags=["kaizen"])
async def commit_changes(request: Request):
    """Commit all staged Kaizen changes to rules.json."""
    kaizen = getattr(request.app.state, "kaizen_engine", None)
    optimizer = getattr(request.app.state, "policy_optimizer", None)
    if kaizen is None:
        raise HTTPException(status_code=503, detail="Kaizen engine not initialised.")

    committed = await kaizen.commit_staged_changes(optimizer=optimizer)
    return {
        "committed": len(committed),
        "entries": [e.to_dict() for e in committed],
    }


@webhook_router.post("/kaizen/rollback", tags=["kaizen"])
async def rollback_changes(request: Request):
    """Discard all pending staged Kaizen changes."""
    kaizen = getattr(request.app.state, "kaizen_engine", None)
    if kaizen is None:
        raise HTTPException(status_code=503, detail="Kaizen engine not initialised.")

    count = await kaizen.rollback_staged_changes()
    return {"rolled_back": count}


@webhook_router.get("/kaizen/evolution-log", tags=["kaizen"])
async def evolution_log(request: Request):
    """View the SCAMPER evolution log."""
    kaizen = getattr(request.app.state, "kaizen_engine", None)
    if kaizen is None:
        raise HTTPException(status_code=503, detail="Kaizen engine not initialised.")

    entries = kaizen.get_evolution_log()
    return {"entries": entries, "count": len(entries)}


@webhook_router.get("/kaizen/muda", tags=["kaizen"])
async def muda_report(request: Request):
    """Run the Muda waste detector and return the report."""
    kaizen = getattr(request.app.state, "kaizen_engine", None)
    if kaizen is None:
        raise HTTPException(status_code=503, detail="Kaizen engine not initialised.")

    report = await kaizen.detect_muda()
    return {
        "latent_tasks": report.latent_tasks,
        "lock_bottlenecks": report.lock_bottlenecks,
        "suggested_reversals": report.suggested_reversals,
        "generated_at": report.generated_at,
    }
