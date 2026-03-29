"""
Tests for HITL — escalation triggers, async wait, timeout, and resolution.
"""

from __future__ import annotations

import asyncio

import pytest
import fakeredis.aioredis

from aegis.core.hitl import (
    ApprovalStatus,
    HITLConfig,
    HITLManager,
    RiskLevel,
)


@pytest.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def hitl_config():
    return HITLConfig(
        cost_threshold_usd=5.0,
        confidence_floor=0.70,
        approval_timeout_seconds=2.0,  # Short timeout for tests.
    )


@pytest.fixture
async def hitl(fake_redis, hitl_config):
    return HITLManager(redis_client=fake_redis, config=hitl_config)


# ---------------------------------------------------------------------------
# Risk Assessment
# ---------------------------------------------------------------------------

class TestRiskAssessment:
    def test_low_risk(self, hitl: HITLManager):
        level, reasons = hitl.assess_risk("summarise document", 1.0, confidence=0.95)
        assert level == RiskLevel.LOW
        assert reasons == []

    def test_medium_risk_near_threshold(self, hitl: HITLManager):
        level, reasons = hitl.assess_risk("run analysis", 3.0, confidence=0.90)
        assert level == RiskLevel.MEDIUM

    def test_high_risk_cost_exceeds_threshold(self, hitl: HITLManager):
        level, reasons = hitl.assess_risk("big batch job", 10.0, confidence=0.95)
        assert level == RiskLevel.HIGH
        assert any("cost" in r.lower() for r in reasons)

    def test_high_risk_sensitive_intent(self, hitl: HITLManager):
        level, reasons = hitl.assess_risk("withdraw funds from vault", 0.50, confidence=0.99)
        assert level == RiskLevel.HIGH
        assert any("sensitive" in r.lower() or "pattern" in r.lower() for r in reasons)

    def test_high_risk_delete_resource(self, hitl: HITLManager):
        level, reasons = hitl.assess_risk("delete resource from production", 0.10)
        assert level == RiskLevel.HIGH

    def test_high_risk_change_admin_policy(self, hitl: HITLManager):
        level, reasons = hitl.assess_risk("change admin policy to permissive", 0.10)
        assert level == RiskLevel.HIGH

    def test_high_risk_low_confidence(self, hitl: HITLManager):
        level, reasons = hitl.assess_risk("classify tickets", 2.0, confidence=0.40)
        assert level == RiskLevel.HIGH
        assert any("confidence" in r.lower() for r in reasons)

    def test_multiple_triggers_stacked(self, hitl: HITLManager):
        """Cost + sensitive intent + low confidence — all three fire."""
        level, reasons = hitl.assess_risk(
            "withdraw funds immediately", 20.0, confidence=0.30
        )
        assert level == RiskLevel.HIGH
        assert len(reasons) == 3


# ---------------------------------------------------------------------------
# Escalation & Timeout
# ---------------------------------------------------------------------------

class TestEscalationTimeout:
    async def test_timeout_auto_rejects(self, hitl: HITLManager):
        """If no human responds within timeout, result is TIMED_OUT."""
        result = await hitl.escalate_and_wait(
            agent_id="bot-risky",
            task_intent="withdraw funds",
            estimated_cost=10.0,
            priority=5,
            confidence=0.50,
            escalation_reasons=["test"],
        )
        assert result.status == ApprovalStatus.TIMED_OUT
        assert result.decided_by == "system:timeout"

    async def test_approval_before_timeout(self, hitl: HITLManager):
        """Human approves within the window — agent gets APPROVED."""

        async def approve_after_delay():
            await asyncio.sleep(0.3)
            # Peek at pending to get the approval_id.
            pending = await hitl.get_pending()
            assert len(pending) == 1
            approval_id = pending[0]["approval_id"]
            resolved = await hitl.resolve(approval_id, approved=True, decided_by="operator_test")
            assert resolved is True

        task = asyncio.create_task(approve_after_delay())
        result = await hitl.escalate_and_wait(
            agent_id="bot-1",
            task_intent="deploy contract",
            estimated_cost=8.0,
            priority=7,
            escalation_reasons=["cost exceeds threshold"],
        )
        await task
        assert result.status == ApprovalStatus.APPROVED
        assert result.decided_by == "operator_test"

    async def test_rejection_before_timeout(self, hitl: HITLManager):
        """Human rejects — agent gets REJECTED."""

        async def reject_after_delay():
            await asyncio.sleep(0.2)
            pending = await hitl.get_pending()
            approval_id = pending[0]["approval_id"]
            await hitl.resolve(approval_id, approved=False, decided_by="security_lead")

        task = asyncio.create_task(reject_after_delay())
        result = await hitl.escalate_and_wait(
            agent_id="bot-2",
            task_intent="drain wallet",
            estimated_cost=50.0,
            priority=3,
            escalation_reasons=["sensitive intent"],
        )
        await task
        assert result.status == ApprovalStatus.REJECTED


# ---------------------------------------------------------------------------
# Concurrent Escalations
# ---------------------------------------------------------------------------

class TestConcurrency:
    async def test_multiple_agents_wait_independently(self, hitl: HITLManager):
        """Two agents escalated simultaneously — approve one, reject the other."""

        results: dict[str, ApprovalStatus] = {}

        async def escalate(agent_id, intent, cost):
            r = await hitl.escalate_and_wait(
                agent_id=agent_id,
                task_intent=intent,
                estimated_cost=cost,
                priority=5,
                escalation_reasons=["concurrent test"],
            )
            results[agent_id] = r.status

        async def resolve_both():
            await asyncio.sleep(0.3)
            pending = await hitl.get_pending()
            assert len(pending) == 2

            # Sort by agent_id for deterministic test.
            pending.sort(key=lambda p: p["agent_id"])

            # Approve agent-A, reject agent-B.
            await hitl.resolve(pending[0]["approval_id"], approved=True, decided_by="op")
            await hitl.resolve(pending[1]["approval_id"], approved=False, decided_by="op")

        t1 = asyncio.create_task(escalate("agent-A", "withdraw funds", 10.0))
        t2 = asyncio.create_task(escalate("agent-B", "delete resource", 6.0))
        t3 = asyncio.create_task(resolve_both())

        await asyncio.gather(t1, t2, t3)

        assert results["agent-A"] == ApprovalStatus.APPROVED
        assert results["agent-B"] == ApprovalStatus.REJECTED

    async def test_resolve_unknown_id_returns_false(self, hitl: HITLManager):
        """Resolving a non-existent approval_id returns False."""
        result = await hitl.resolve("nonexistent_id_12345", approved=True)
        assert result is False


# ---------------------------------------------------------------------------
# Redis Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    async def test_pending_stored_in_redis(self, hitl: HITLManager, fake_redis):
        """Escalation should appear in Redis while waiting."""

        async def check_redis():
            await asyncio.sleep(0.2)
            pending = await hitl.get_pending()
            assert len(pending) == 1
            assert pending[0]["agent_id"] == "bot-persist"
            # Let it time out.

        task = asyncio.create_task(check_redis())
        await hitl.escalate_and_wait(
            agent_id="bot-persist",
            task_intent="withdraw funds",
            estimated_cost=15.0,
            priority=5,
            escalation_reasons=["persistence test"],
        )
        await task

    async def test_resolved_clears_pending(self, hitl: HITLManager, fake_redis):
        """After resolution, pending key should be deleted."""

        async def approve():
            await asyncio.sleep(0.2)
            pending = await hitl.get_pending()
            await hitl.resolve(pending[0]["approval_id"], approved=True)

        task = asyncio.create_task(approve())
        await hitl.escalate_and_wait(
            agent_id="bot-clear",
            task_intent="withdraw funds",
            estimated_cost=7.0,
            priority=5,
            escalation_reasons=["clear test"],
        )
        await task

        # Pending list should now be empty.
        pending = await hitl.get_pending()
        assert len(pending) == 0
