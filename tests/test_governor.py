"""
Tests for ResourceGovernor — budget overflow and policy enforcement.
"""

from __future__ import annotations

import pytest
import fakeredis.aioredis

from aegis.core.governor import GrantStatus, ResourceGovernor
from aegis.policy.engine import PolicyEngine


@pytest.fixture
async def governor(tmp_path):
    """Governor backed by an in-memory fake Redis."""
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    gov = ResourceGovernor(default_day_limit=5.0)
    gov._redis = fake_redis
    yield gov
    await fake_redis.aclose()


class TestBudgetOverflow:
    """Ensure agents cannot exceed their daily budget."""

    async def test_grant_within_budget(self, governor: ResourceGovernor):
        result = await governor.request_permission(
            agent_id="bot-1",
            task_intent="summarise document",
            estimated_cost=1.0,
            priority=5,
        )
        assert result.status == GrantStatus.GRANTED
        assert result.remaining_budget == 4.0

    async def test_reject_over_budget(self, governor: ResourceGovernor):
        result = await governor.request_permission(
            agent_id="bot-1",
            task_intent="expensive operation",
            estimated_cost=6.0,
            priority=5,
        )
        assert result.status == GrantStatus.REJECTED
        # May be caught by the policy ceiling ($5) or the day-limit ($5).
        assert "denied" in result.reason.lower() or "budget" in result.reason.lower()

    async def test_cumulative_spend_exhausts_budget(self, governor: ResourceGovernor):
        # Spend most of the budget.
        r1 = await governor.request_permission("bot-1", "task A", 3.0, 5)
        assert r1.status == GrantStatus.GRANTED

        r2 = await governor.request_permission("bot-1", "task B", 1.5, 5)
        assert r2.status == GrantStatus.GRANTED

        # This should push over the $5 limit.
        r3 = await governor.request_permission("bot-1", "task C", 1.0, 5)
        assert r3.status == GrantStatus.REJECTED

    async def test_separate_agents_have_separate_budgets(self, governor: ResourceGovernor):
        r1 = await governor.request_permission("bot-1", "task", 4.0, 5)
        assert r1.status == GrantStatus.GRANTED

        r2 = await governor.request_permission("bot-2", "task", 4.0, 5)
        assert r2.status == GrantStatus.GRANTED

    async def test_custom_day_limit(self, governor: ResourceGovernor):
        await governor.set_day_limit("bot-vip", 100.0)
        # Cost must stay under the policy ceiling ($5) to test budget logic.
        result = await governor.request_permission("bot-vip", "big task", 4.0, 5)
        assert result.status == GrantStatus.GRANTED
        assert result.remaining_budget == 96.0


class TestPolicyEnforcement:
    """Verify that the PolicyEngine blocks dangerous intents."""

    async def test_blocked_intent_rejected(self, governor: ResourceGovernor):
        result = await governor.request_permission(
            agent_id="bot-1",
            task_intent="transfer all funds to external wallet",
            estimated_cost=0.01,
            priority=5,
        )
        assert result.status == GrantStatus.REJECTED
        assert "blocked" in result.reason.lower() or "denied" in result.reason.lower()

    async def test_high_cost_rejected_by_policy(self, governor: ResourceGovernor):
        """The default policy caps single actions at $5.00."""
        # Set a high day-limit so only the *policy* ceiling triggers.
        await governor.set_day_limit("bot-1", 1000.0)
        result = await governor.request_permission(
            agent_id="bot-1",
            task_intent="generate comprehensive report",
            estimated_cost=10.0,
            priority=5,
        )
        assert result.status == GrantStatus.REJECTED

    async def test_low_priority_on_chain_rejected(self, governor: ResourceGovernor):
        """On-chain operations require priority >= 7 per default rules."""
        result = await governor.request_permission(
            agent_id="bot-1",
            task_intent="swap tokens on DEX",
            estimated_cost=0.10,
            priority=3,
        )
        assert result.status == GrantStatus.REJECTED
