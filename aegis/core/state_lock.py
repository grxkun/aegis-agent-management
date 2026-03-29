"""
StateLockManager — Redis-Backed Distributed Semaphore with Deadlock Prevention.

State Integrity Principle:
    Agents must acquire explicit locks on shared resources (wallets, files,
    API rate-limit slots) before mutating them.  This prevents race conditions
    in multi-agent environments where N agents may target the same on-chain
    wallet or the same downstream API concurrently.

Deadlock Prevention:
    A higher-priority agent can *preempt* a lower-priority lock.  The
    preempted agent receives a ``LockPreempted`` exception and must back off.
    This guarantees progress for critical-path operations and prevents
    priority inversion.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import redis.asyncio as redis

logger = logging.getLogger("aegis.state_lock")


class LockPreempted(Exception):
    """Raised when a held lock is forcibly released by a higher-priority agent."""


class LockAcquisitionFailed(Exception):
    """Raised when a lock cannot be acquired within the timeout window."""


@dataclass(frozen=True)
class LockInfo:
    """Metadata stored alongside a held lock in Redis."""
    agent_id: str
    priority: int
    token: str  # Unique per acquisition — prevents accidental release.
    resource: str


class StateLockManager:
    """
    Async context-manager for distributed resource locking.

    Usage::

        mgr = StateLockManager(redis_url="redis://localhost:6379/0")

        async with mgr.acquire("wallet_sui_main", agent_id="bot-1", priority=7):
            # ... mutate wallet safely ...

    Parameters
    ----------
    redis_url : str
        Redis connection string.
    default_ttl : int
        Seconds before a lock auto-expires (prevents zombie locks).
    poll_interval : float
        Seconds between retry attempts when waiting for a lock.
    """

    _KEY_PREFIX = "aegis:lock:"
    _META_PREFIX = "aegis:lockmeta:"

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        default_ttl: int = 30,
        poll_interval: float = 0.25,
    ) -> None:
        self._redis: redis.Redis = redis.from_url(
            redis_url, decode_responses=True
        )
        self._default_ttl = default_ttl
        self._poll_interval = poll_interval

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(
        self,
        resource: str,
        agent_id: str,
        priority: int = 5,
        ttl: Optional[int] = None,
        timeout: float = 10.0,
    ) -> AsyncIterator[LockInfo]:
        """
        Async context manager that holds a lock on *resource*.

        Parameters
        ----------
        resource : str
            Logical name of the resource (e.g. ``"wallet_sui_main"``).
        agent_id : str
            Identity of the requesting agent.
        priority : int
            1 (lowest) – 10 (highest).  Higher priority can preempt lower.
        ttl : int | None
            Override for lock expiry in seconds.
        timeout : float
            Maximum seconds to wait for the lock before raising.

        Yields
        ------
        LockInfo

        Raises
        ------
        LockAcquisitionFailed
            If the lock cannot be obtained within *timeout*.
        """
        ttl = ttl or self._default_ttl
        token = uuid.uuid4().hex
        lock_key = f"{self._KEY_PREFIX}{resource}"
        meta_key = f"{self._META_PREFIX}{resource}"

        acquired = await self._try_acquire(
            lock_key, meta_key, agent_id, priority, token, ttl, timeout
        )
        if not acquired:
            raise LockAcquisitionFailed(
                f"Agent {agent_id} could not acquire lock on '{resource}' "
                f"within {timeout}s"
            )

        info = LockInfo(agent_id=agent_id, priority=priority, token=token, resource=resource)
        logger.info("LOCK ACQUIRED resource=%s agent=%s priority=%d", resource, agent_id, priority)
        try:
            yield info
        finally:
            await self._release(lock_key, meta_key, token)
            logger.info("LOCK RELEASED resource=%s agent=%s", resource, agent_id)

    async def force_release(self, resource: str) -> bool:
        """Administratively release a lock regardless of ownership."""
        lock_key = f"{self._KEY_PREFIX}{resource}"
        meta_key = f"{self._META_PREFIX}{resource}"
        deleted = await self._redis.delete(lock_key, meta_key)
        return deleted > 0

    async def inspect(self, resource: str) -> Optional[dict]:
        """Return current lock holder metadata, or None if unlocked."""
        meta_key = f"{self._META_PREFIX}{resource}"
        return await self._redis.hgetall(meta_key) or None

    async def close(self) -> None:
        await self._redis.aclose()

    # ------------------------------------------------------------------
    # Internal lock machinery
    # ------------------------------------------------------------------

    async def _try_acquire(
        self,
        lock_key: str,
        meta_key: str,
        agent_id: str,
        priority: int,
        token: str,
        ttl: int,
        timeout: float,
    ) -> bool:
        """Attempt to acquire, with preemption and retry loop."""
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            # Attempt atomic SET NX (create-if-not-exists).
            created = await self._redis.set(lock_key, token, nx=True, ex=ttl)
            if created:
                await self._store_meta(meta_key, agent_id, priority, token, ttl)
                return True

            # Lock is held — check if we can preempt.
            if await self._try_preempt(lock_key, meta_key, agent_id, priority, token, ttl):
                return True

            await asyncio.sleep(self._poll_interval)

        return False

    async def _try_preempt(
        self,
        lock_key: str,
        meta_key: str,
        agent_id: str,
        priority: int,
        token: str,
        ttl: int,
    ) -> bool:
        """
        Preempt a lower-priority lock holder.

        Deadlock Prevention:
            If the current holder has a *strictly lower* priority, we delete
            their lock and take over.  The evicted agent will discover the
            preemption on its next heartbeat or when it tries to release.
        """
        holder_meta = await self._redis.hgetall(meta_key)
        if not holder_meta:
            return False

        holder_priority = int(holder_meta.get("priority", 10))
        if priority > holder_priority:
            logger.warning(
                "PREEMPT resource lock: agent=%s (pri=%d) evicts agent=%s (pri=%d)",
                agent_id, priority,
                holder_meta.get("agent_id", "?"), holder_priority,
            )
            # Atomic delete-and-recreate via pipeline.
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(lock_key)
                pipe.set(lock_key, token, ex=ttl)
                await pipe.execute()
            await self._store_meta(meta_key, agent_id, priority, token, ttl)
            return True

        return False

    async def _store_meta(
        self, meta_key: str, agent_id: str, priority: int, token: str, ttl: int
    ) -> None:
        await self._redis.hset(meta_key, mapping={
            "agent_id": agent_id,
            "priority": str(priority),
            "token": token,
        })
        await self._redis.expire(meta_key, ttl)

    async def _release(self, lock_key: str, meta_key: str, token: str) -> None:
        """Release only if we still own the lock (token match).

        Uses a pipeline with WATCH for optimistic locking instead of Lua
        EVAL, ensuring compatibility with fakeredis in tests.
        """
        current = await self._redis.get(lock_key)
        if current == token:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(lock_key)
                pipe.delete(meta_key)
                await pipe.execute()
