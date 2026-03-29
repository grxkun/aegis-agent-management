"""
Kaizen Engine — Continuous Improvement via SCAMPER Framework.

Implements the SCAMPER (Substitute, Combine, Adapt, Modify, Put to other use,
Eliminate, Reverse) optimisation cycle for Aegis resource governance.

Core Capabilities:

1. **Shadow Mode (Substitute)**:
   Run an experimental model/strategy alongside the control.  Compare
   accuracy vs. cost.  If the experiment is >=95% as effective but >=30%
   cheaper, promote it automatically.

2. **Muda Detector (Eliminate)**:
   Track latent tasks (requested but never completed) and resource-lock
   wait times.  Surface bottlenecks and suggest priority reversals.

3. **SCAMPER Log (evolution_log.json)**:
   Every self-learning change is recorded with the SCAMPER type, previous
   policy, new policy, and efficiency gain.

4. **Kaizen Summary**:
   A 24-hour digest sent to the Telegram bot with [ COMMIT ] / [ ROLLBACK ].
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import redis.asyncio as redis

logger = logging.getLogger("aegis.kaizen")

_DEFAULT_EVOLUTION_LOG = Path(__file__).parent.parent.parent / "evolution_log.json"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class ScamperType(str, Enum):
    SUBSTITUTE = "Substitute"
    COMBINE = "Combine"
    ADAPT = "Adapt"
    MODIFY = "Modify"
    PUT_TO_OTHER_USE = "PutToOtherUse"
    ELIMINATE = "Eliminate"
    REVERSE = "Reverse"


class ExperimentStatus(str, Enum):
    RUNNING = "RUNNING"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    PENDING_REVIEW = "PENDING_REVIEW"


@dataclass
class ExperimentResult:
    """Outcome of a shadow-mode experiment."""
    experiment_id: str
    scamper_type: ScamperType
    control_model: str
    experiment_model: str
    control_cost: float
    experiment_cost: float
    control_quality: float  # 0.0–1.0
    experiment_quality: float  # 0.0–1.0
    cost_savings_pct: float
    quality_retention_pct: float
    status: ExperimentStatus
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class EvolutionEntry:
    """A single entry in the SCAMPER evolution log."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    scamper_type: str = ""
    previous_policy: str = ""
    new_policy: str = ""
    efficiency_gain: str = ""
    experiment_id: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    committed: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "SCAMPER_TYPE": self.scamper_type,
            "PREVIOUS_POLICY": self.previous_policy,
            "NEW_POLICY": self.new_policy,
            "EFFICIENCY_GAIN": self.efficiency_gain,
            "experiment_id": self.experiment_id,
            "timestamp": self.timestamp,
            "committed": self.committed,
        }


@dataclass
class MudaReport:
    """Waste analysis output from the Muda Detector."""
    latent_tasks: list[dict] = field(default_factory=list)
    lock_bottlenecks: list[dict] = field(default_factory=list)
    suggested_reversals: list[dict] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------------------
# Kaizen Engine
# ---------------------------------------------------------------------------

class KaizenEngine:
    """
    SCAMPER-based continuous improvement engine.

    Sits alongside the ResourceGovernor and ModelArbiter to run experiments,
    detect waste, and evolve routing policies autonomously.

    Parameters
    ----------
    redis_client : redis.Redis
        Shared async Redis connection.
    evolution_log_path : Path
        Where to persist the SCAMPER evolution log.
    quality_threshold : float
        Minimum quality retention (0.0–1.0) for auto-promotion.
    cost_threshold : float
        Minimum cost savings (0.0–1.0) for auto-promotion.
    auto_promote : bool
        If True, experiments that pass thresholds are promoted without
        human review.  If False, they go to PENDING_REVIEW.
    """

    _KEY_TASK_START = "aegis:kaizen:task_start:{agent_id}:{task_id}"
    _KEY_TASK_COMPLETE = "aegis:kaizen:task_complete:{agent_id}:{task_id}"
    _KEY_LOCK_WAIT = "aegis:kaizen:lock_wait:{resource}"
    _KEY_EXPERIMENT = "aegis:kaizen:experiment:{experiment_id}"

    def __init__(
        self,
        redis_client: redis.Redis,
        evolution_log_path: Path = _DEFAULT_EVOLUTION_LOG,
        quality_threshold: float = 0.95,
        cost_threshold: float = 0.30,
        auto_promote: bool = False,
    ) -> None:
        self._redis = redis_client
        self._log_path = evolution_log_path
        self._quality_threshold = quality_threshold
        self._cost_threshold = cost_threshold
        self._auto_promote = auto_promote

        # In-memory experiment registry.
        self._experiments: dict[str, dict[str, Any]] = {}
        # Staged changes awaiting human commit/rollback.
        self._staged_changes: list[EvolutionEntry] = []

    # ------------------------------------------------------------------
    # Shadow Mode — Substitute experiments
    # ------------------------------------------------------------------

    async def start_experiment(
        self,
        control_model: str,
        experiment_model: str,
        logic_density: int,
        description: str = "",
    ) -> str:
        """
        Begin a shadow-mode experiment comparing two models.

        Returns the experiment_id.  Callers should report results via
        ``record_observation()``.
        """
        experiment_id = uuid.uuid4().hex[:16]
        experiment = {
            "experiment_id": experiment_id,
            "control_model": control_model,
            "experiment_model": experiment_model,
            "logic_density": logic_density,
            "description": description,
            "observations": [],
            "status": ExperimentStatus.RUNNING.value,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._experiments[experiment_id] = experiment

        # Persist to Redis for cross-process visibility.
        key = self._KEY_EXPERIMENT.format(experiment_id=experiment_id)
        await self._redis.set(key, json.dumps(experiment), ex=86400 * 7)

        logger.info(
            "EXPERIMENT STARTED id=%s control=%s experiment=%s density=%d",
            experiment_id, control_model, experiment_model, logic_density,
        )
        return experiment_id

    async def record_observation(
        self,
        experiment_id: str,
        is_control: bool,
        quality_score: float,
        cost_usd: float,
        latency_ms: float = 0.0,
    ) -> None:
        """
        Record a single observation (control or experiment arm).

        Quality score is application-defined (e.g. accuracy, user rating).
        """
        experiment = self._experiments.get(experiment_id)
        if not experiment:
            logger.warning("Unknown experiment_id=%s", experiment_id)
            return

        experiment["observations"].append({
            "arm": "control" if is_control else "experiment",
            "quality_score": quality_score,
            "cost_usd": cost_usd,
            "latency_ms": latency_ms,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def evaluate_experiment(self, experiment_id: str) -> Optional[ExperimentResult]:
        """
        Evaluate an experiment's observations and decide: promote, reject,
        or stage for human review.

        Promotion criteria:
            - Experiment quality >= 95% of control quality.
            - Experiment cost <= 70% of control cost (i.e. 30%+ cheaper).
        """
        experiment = self._experiments.get(experiment_id)
        if not experiment:
            return None

        observations = experiment["observations"]
        control_obs = [o for o in observations if o["arm"] == "control"]
        experiment_obs = [o for o in observations if o["arm"] == "experiment"]

        if not control_obs or not experiment_obs:
            logger.info("Experiment %s: insufficient observations.", experiment_id)
            return None

        # Aggregate.
        avg_control_quality = sum(o["quality_score"] for o in control_obs) / len(control_obs)
        avg_experiment_quality = sum(o["quality_score"] for o in experiment_obs) / len(experiment_obs)
        avg_control_cost = sum(o["cost_usd"] for o in control_obs) / len(control_obs)
        avg_experiment_cost = sum(o["cost_usd"] for o in experiment_obs) / len(experiment_obs)

        # Ratios.
        quality_retention = (
            avg_experiment_quality / avg_control_quality
            if avg_control_quality > 0 else 0.0
        )
        cost_savings = (
            1.0 - (avg_experiment_cost / avg_control_cost)
            if avg_control_cost > 0 else 0.0
        )

        # Decision.
        passes_quality = quality_retention >= self._quality_threshold
        passes_cost = cost_savings >= self._cost_threshold

        if passes_quality and passes_cost:
            status = (
                ExperimentStatus.PROMOTED if self._auto_promote
                else ExperimentStatus.PENDING_REVIEW
            )
        else:
            status = ExperimentStatus.REJECTED

        result = ExperimentResult(
            experiment_id=experiment_id,
            scamper_type=ScamperType.SUBSTITUTE,
            control_model=experiment["control_model"],
            experiment_model=experiment["experiment_model"],
            control_cost=avg_control_cost,
            experiment_cost=avg_experiment_cost,
            control_quality=avg_control_quality,
            experiment_quality=avg_experiment_quality,
            cost_savings_pct=round(cost_savings * 100, 2),
            quality_retention_pct=round(quality_retention * 100, 2),
            status=status,
        )

        experiment["status"] = status.value
        logger.info(
            "EXPERIMENT EVALUATED id=%s status=%s quality_retention=%.1f%% cost_savings=%.1f%%",
            experiment_id, status.value,
            quality_retention * 100, cost_savings * 100,
        )

        # Stage for evolution log.
        if status in (ExperimentStatus.PROMOTED, ExperimentStatus.PENDING_REVIEW):
            entry = EvolutionEntry(
                scamper_type=ScamperType.SUBSTITUTE.value,
                previous_policy=(
                    f"Use {experiment['control_model']} for "
                    f"logic_density={experiment['logic_density']}"
                ),
                new_policy=(
                    f"Use {experiment['experiment_model']} for "
                    f"logic_density={experiment['logic_density']}"
                ),
                efficiency_gain=f"+{result.cost_savings_pct:.1f}% cost savings, "
                                f"{result.quality_retention_pct:.1f}% quality retained",
                experiment_id=experiment_id,
                committed=self._auto_promote,
            )
            self._staged_changes.append(entry)
            if self._auto_promote:
                self._write_evolution_log(entry)

        return result

    # ------------------------------------------------------------------
    # Muda Detector — Eliminate waste
    # ------------------------------------------------------------------

    async def record_task_start(self, agent_id: str, task_id: str) -> None:
        """Record that a task has been started (for latent-task tracking)."""
        key = self._KEY_TASK_START.format(agent_id=agent_id, task_id=task_id)
        await self._redis.set(key, str(time.time()), ex=86400)

    async def record_task_complete(self, agent_id: str, task_id: str) -> None:
        """Record that a task completed successfully."""
        key = self._KEY_TASK_COMPLETE.format(agent_id=agent_id, task_id=task_id)
        await self._redis.set(key, str(time.time()), ex=86400)

    async def record_lock_wait(
        self, resource: str, agent_id: str, wait_seconds: float
    ) -> None:
        """Record how long an agent waited for a resource lock."""
        key = self._KEY_LOCK_WAIT.format(resource=resource)
        entry = json.dumps({
            "agent_id": agent_id,
            "wait_seconds": wait_seconds,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await self._redis.lpush(key, entry)
        await self._redis.ltrim(key, 0, 99)  # Keep last 100.
        await self._redis.expire(key, 86400)

    async def detect_muda(self) -> MudaReport:
        """
        Scan Redis for waste signals and produce a report.

        Detects:
            - **Latent tasks**: started but never completed (zombie tasks).
            - **Lock bottlenecks**: resources where average wait > 5s.
            - **Priority reversals**: lower-priority agents blocking higher ones.
        """
        report = MudaReport()

        # ── Latent tasks ─────────────────────────────────────────────
        start_keys: list[str] = []
        async for key in self._redis.scan_iter("aegis:kaizen:task_start:*"):
            start_keys.append(key)

        for key in start_keys:
            # key format: aegis:kaizen:task_start:{agent_id}:{task_id}
            parts = key.split(":")
            if len(parts) < 5:
                continue
            agent_id, task_id = parts[3], parts[4]
            complete_key = self._KEY_TASK_COMPLETE.format(
                agent_id=agent_id, task_id=task_id
            )
            if not await self._redis.exists(complete_key):
                start_time = await self._redis.get(key)
                elapsed = time.time() - float(start_time) if start_time else 0
                report.latent_tasks.append({
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "elapsed_seconds": round(elapsed, 1),
                    "status": "NEVER_COMPLETED",
                })

        # ── Lock bottlenecks ─────────────────────────────────────────
        lock_keys: list[str] = []
        async for key in self._redis.scan_iter("aegis:kaizen:lock_wait:*"):
            lock_keys.append(key)

        for key in lock_keys:
            resource = key.split(":")[-1]
            raw_entries = await self._redis.lrange(key, 0, -1)
            if not raw_entries:
                continue

            waits = []
            agents_waiting = set()
            for raw in raw_entries:
                entry = json.loads(raw)
                waits.append(entry["wait_seconds"])
                agents_waiting.add(entry["agent_id"])

            avg_wait = sum(waits) / len(waits)
            if avg_wait > 5.0:  # Bottleneck threshold.
                report.lock_bottlenecks.append({
                    "resource": resource,
                    "avg_wait_seconds": round(avg_wait, 2),
                    "max_wait_seconds": round(max(waits), 2),
                    "affected_agents": list(agents_waiting),
                    "observation_count": len(waits),
                })
                # Suggest a reversal if low-priority agents cause the jam.
                report.suggested_reversals.append({
                    "scamper_type": ScamperType.REVERSE.value,
                    "resource": resource,
                    "suggestion": (
                        f"Resource '{resource}' has avg wait {avg_wait:.1f}s. "
                        f"Consider raising the default TTL or adjusting lock "
                        f"priority policy for affected agents."
                    ),
                })

        logger.info(
            "MUDA REPORT: %d latent tasks, %d lock bottlenecks",
            len(report.latent_tasks), len(report.lock_bottlenecks),
        )
        return report

    # ------------------------------------------------------------------
    # Kaizen Summary (24-hour digest)
    # ------------------------------------------------------------------

    async def generate_kaizen_summary(self) -> dict:
        """
        Produce a 24-hour digest of all experiments, waste signals, and
        staged policy changes.  This is the payload for the Telegram summary.
        """
        muda = await self.detect_muda()

        promoted = [e for e in self._staged_changes if e.committed]
        pending = [e for e in self._staged_changes if not e.committed]

        experiments = []
        for exp in self._experiments.values():
            experiments.append({
                "id": exp["experiment_id"],
                "control": exp["control_model"],
                "experiment": exp["experiment_model"],
                "status": exp["status"],
            })

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "experiments": experiments,
            "staged_changes": [e.to_dict() for e in pending],
            "committed_changes": [e.to_dict() for e in promoted],
            "muda_report": {
                "latent_tasks": len(muda.latent_tasks),
                "lock_bottlenecks": len(muda.lock_bottlenecks),
                "suggested_reversals": muda.suggested_reversals,
            },
        }

    def format_telegram_summary(self, summary: dict) -> str:
        """Format the Kaizen summary as a Telegram-friendly message."""
        lines = [
            "\U0001f4ca *Kaizen Summary (24h)*",
            "\u2500" * 24,
        ]

        # Experiments.
        exps = summary.get("experiments", [])
        if exps:
            lines.append(f"\n\U0001f52c *Experiments:* {len(exps)}")
            for e in exps:
                icon = {
                    "PROMOTED": "\u2705",
                    "REJECTED": "\u274c",
                    "PENDING_REVIEW": "\u23f3",
                    "RUNNING": "\u25b6\ufe0f",
                }.get(e["status"], "\u2753")
                lines.append(
                    f"  {icon} `{e['id'][:8]}` {e['control']} \u2192 {e['experiment']} "
                    f"[{e['status']}]"
                )

        # Staged changes.
        staged = summary.get("staged_changes", [])
        if staged:
            lines.append(f"\n\U0001f4dd *Pending Changes:* {len(staged)}")
            for s in staged:
                lines.append(
                    f"  \u2022 [{s['SCAMPER_TYPE']}] {s['NEW_POLICY']}\n"
                    f"    Gain: {s['EFFICIENCY_GAIN']}"
                )

        # Muda.
        muda = summary.get("muda_report", {})
        latent = muda.get("latent_tasks", 0)
        bottlenecks = muda.get("lock_bottlenecks", 0)
        if latent or bottlenecks:
            lines.append(f"\n\u26a0\ufe0f *Waste Detected:*")
            if latent:
                lines.append(f"  \u2022 {latent} latent (zombie) tasks")
            if bottlenecks:
                lines.append(f"  \u2022 {bottlenecks} lock bottleneck(s)")
            for rev in muda.get("suggested_reversals", []):
                lines.append(f"  \U0001f504 {rev['suggestion']}")

        if not exps and not staged and not latent and not bottlenecks:
            lines.append("\n\u2705 System is operating efficiently. No changes needed.")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Commit / Rollback
    # ------------------------------------------------------------------

    async def commit_staged_changes(self, optimizer=None) -> list[EvolutionEntry]:
        """
        Commit all pending staged changes.

        If an ``optimizer`` (PolicyOptimizer) is provided, it will rewrite
        the rules.json accordingly.
        """
        committed = []
        for entry in self._staged_changes:
            if not entry.committed:
                entry.committed = True
                self._write_evolution_log(entry)
                committed.append(entry)

        if optimizer and committed:
            for entry in committed:
                await optimizer.apply_evolution(entry)

        self._staged_changes = [e for e in self._staged_changes if not e.committed]
        logger.info("KAIZEN COMMITTED %d changes.", len(committed))
        return committed

    async def rollback_staged_changes(self) -> int:
        """Discard all pending staged changes."""
        count = len([e for e in self._staged_changes if not e.committed])
        self._staged_changes = [e for e in self._staged_changes if e.committed]
        logger.info("KAIZEN ROLLED BACK %d pending changes.", count)
        return count

    # ------------------------------------------------------------------
    # Evolution log persistence
    # ------------------------------------------------------------------

    def _write_evolution_log(self, entry: EvolutionEntry) -> None:
        """Append an entry to evolution_log.json."""
        log_data = {"entries": []}
        if self._log_path.exists():
            try:
                with open(self._log_path) as f:
                    log_data = json.load(f)
            except (json.JSONDecodeError, KeyError):
                log_data = {"entries": []}

        log_data["entries"].append(entry.to_dict())

        with open(self._log_path, "w") as f:
            json.dump(log_data, f, indent=2)

        logger.info(
            "EVOLUTION LOG: [%s] %s -> %s (%s)",
            entry.scamper_type, entry.previous_policy,
            entry.new_policy, entry.efficiency_gain,
        )

    def get_evolution_log(self) -> list[dict]:
        """Read and return all entries from the evolution log."""
        if not self._log_path.exists():
            return []
        try:
            with open(self._log_path) as f:
                data = json.load(f)
            return data.get("entries", [])
        except (json.JSONDecodeError, KeyError):
            return []
