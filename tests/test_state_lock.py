"""
Tests for StateLockManager — deadlock prevention and preemption.
"""

from __future__ import annotations

import asyncio

import pytest
import fakeredis.aioredis

from aegis.core.state_lock import (
    LockAcquisitionFailed,
    StateLockManager,
)


@pytest.fixture
async def lock_mgr():
    """Lock manager backed by fake Redis."""
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    mgr = StateLockManager(default_ttl=5, poll_interval=0.05)
    mgr._redis = fake_redis
    yield mgr
    await fake_redis.aclose()


class TestBasicLocking:
    async def test_acquire_and_release(self, lock_mgr: StateLockManager):
        async with lock_mgr.acquire("wallet_sui_main", agent_id="bot-1", priority=5) as info:
            assert info.resource == "wallet_sui_main"
            assert info.agent_id == "bot-1"
            # Lock should be visible in Redis.
            meta = await lock_mgr.inspect("wallet_sui_main")
            assert meta is not None
            assert meta["agent_id"] == "bot-1"

        # After context exit, lock should be released.
        meta = await lock_mgr.inspect("wallet_sui_main")
        assert meta is None

    async def test_timeout_when_locked(self, lock_mgr: StateLockManager):
        async with lock_mgr.acquire("res-A", agent_id="bot-1", priority=5):
            with pytest.raises(LockAcquisitionFailed):
                async with lock_mgr.acquire(
                    "res-A", agent_id="bot-2", priority=5, timeout=0.3
                ):
                    pass  # Should never reach here.


class TestDeadlockPrevention:
    async def test_high_priority_preempts_low(self, lock_mgr: StateLockManager):
        """A priority-8 agent should evict a priority-3 holder."""
        # bot-1 (low priority) takes the lock.
        async with lock_mgr.acquire("wallet", agent_id="bot-1", priority=3):
            # bot-2 (high priority) should preempt.
            async with lock_mgr.acquire(
                "wallet", agent_id="bot-2", priority=8, timeout=1.0
            ) as info:
                assert info.agent_id == "bot-2"
                meta = await lock_mgr.inspect("wallet")
                assert meta["agent_id"] == "bot-2"

    async def test_equal_priority_does_not_preempt(self, lock_mgr: StateLockManager):
        """Same-priority agents must wait — no preemption."""
        async with lock_mgr.acquire("wallet", agent_id="bot-1", priority=5):
            with pytest.raises(LockAcquisitionFailed):
                async with lock_mgr.acquire(
                    "wallet", agent_id="bot-2", priority=5, timeout=0.3
                ):
                    pass

    async def test_force_release(self, lock_mgr: StateLockManager):
        async with lock_mgr.acquire("res-X", agent_id="bot-1", priority=5):
            released = await lock_mgr.force_release("res-X")
            assert released is True
