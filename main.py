"""
Aegis — Entry Point.

Starts the FastAPI Governor API with all subsystems wired together.

Usage:
    uvicorn main:app --reload --port 8000
    # or
    python main.py
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from aegis import __version__
from aegis.api.routes import router
from aegis.api.webhook import webhook_router
from aegis.core.arbiter import ModelArbiter
from aegis.core.governor import ResourceGovernor
from aegis.core.hitl import HITLConfig, HITLManager
from aegis.core.kaizen import KaizenEngine
from aegis.core.state_lock import StateLockManager
from aegis.policy.optimizer import PolicyOptimizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)-30s | %(levelname)-7s | %(message)s",
)
logger = logging.getLogger("aegis")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DEFAULT_DAY_LIMIT = float(os.getenv("DEFAULT_DAY_LIMIT", "10.0"))
HOST = os.getenv("AEGIS_HOST", "0.0.0.0")
PORT = int(os.getenv("AEGIS_PORT", "8000"))
USE_FAKE_REDIS = os.getenv("USE_FAKE_REDIS", "false").lower() in ("1", "true", "yes")

# HITL / Telegram
HITL_COST_THRESHOLD = float(os.getenv("HITL_COST_THRESHOLD", "5.0"))
HITL_TIMEOUT_SECONDS = float(os.getenv("HITL_TIMEOUT_SECONDS", "600"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_OPERATOR_CHAT_ID = os.getenv("TELEGRAM_OPERATOR_CHAT_ID", "")
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "")


def _make_fake_redis():
    """Create an in-memory fake Redis for local dev without a Redis server."""
    import fakeredis.aioredis
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Wire up and tear down all Governor subsystems including HITL."""
    governor = ResourceGovernor(redis_url=REDIS_URL, default_day_limit=DEFAULT_DAY_LIMIT)
    arbiter = ModelArbiter()
    lock_manager = StateLockManager(redis_url=REDIS_URL)

    if USE_FAKE_REDIS:
        fake = _make_fake_redis()
        governor._redis = fake
        lock_manager._redis = fake
        logger.info("Using in-memory FakeRedis (dev mode)")

    # ── Kaizen Engine ───────────────────────────────────────────
    policy_optimizer = PolicyOptimizer()
    kaizen_engine = KaizenEngine(redis_client=governor._redis)

    # ── HITL Manager ─────────────────────────────────────────────
    hitl_config = HITLConfig(
        cost_threshold_usd=HITL_COST_THRESHOLD,
        approval_timeout_seconds=HITL_TIMEOUT_SECONDS,
    )
    hitl_manager = HITLManager(
        redis_client=governor._redis,
        config=hitl_config,
    )

    # ── Telegram Bot (optional) ──────────────────────────────────
    telegram_bot = None
    if TELEGRAM_BOT_TOKEN and TELEGRAM_OPERATOR_CHAT_ID:
        from aegis.adapters.telegram_bot import TelegramApprovalBot

        telegram_bot = TelegramApprovalBot(
            token=TELEGRAM_BOT_TOKEN,
            operator_chat_id=TELEGRAM_OPERATOR_CHAT_ID,
            hitl_manager=hitl_manager,
            webhook_url=TELEGRAM_WEBHOOK_URL or None,
            kaizen_engine=kaizen_engine,
            policy_optimizer=policy_optimizer,
        )
        # Wire the bot as the HITL notifier.
        hitl_manager._notifier = telegram_bot.send_escalation
        await telegram_bot.setup()
        logger.info("Telegram approval bot connected (chat_id=%s)", TELEGRAM_OPERATOR_CHAT_ID)
    else:
        logger.info("Telegram bot not configured — HITL approvals via /api/v1/hitl/resolve only")

    app.state.governor = governor
    app.state.arbiter = arbiter
    app.state.lock_manager = lock_manager
    app.state.hitl_manager = hitl_manager
    app.state.telegram_bot = telegram_bot
    app.state.kaizen_engine = kaizen_engine
    app.state.policy_optimizer = policy_optimizer

    logger.info("Aegis v%s online — Governor ready on %s:%d", __version__, HOST, PORT)
    yield

    if telegram_bot:
        await telegram_bot.shutdown()
    await governor.close()
    await lock_manager.close()
    logger.info("Aegis shutdown complete.")


app = FastAPI(
    title="Aegis — Resource Governor",
    description=(
        "Control Plane for multi-agent budget, compute, and shared-state governance. "
        "Sits between human intent and agentic execution."
    ),
    version=__version__,
    lifespan=lifespan,
)

app.include_router(router, prefix="/api/v1")
app.include_router(webhook_router, prefix="/api/v1")


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=True)
