"""
Tests for the Kaizen Engine — SCAMPER experiments, Muda detection, evolution log.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import fakeredis.aioredis

from aegis.core.kaizen import (
    ExperimentStatus,
    KaizenEngine,
    ScamperType,
)
from aegis.policy.optimizer import PolicyOptimizer


@pytest.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def evolution_log(tmp_path) -> Path:
    return tmp_path / "evolution_log.json"


@pytest.fixture
async def kaizen(fake_redis, evolution_log):
    return KaizenEngine(
        redis_client=fake_redis,
        evolution_log_path=evolution_log,
        quality_threshold=0.95,
        cost_threshold=0.30,
        auto_promote=False,
    )


@pytest.fixture
def optimizer(tmp_path) -> PolicyOptimizer:
    # Copy the default rules to a temp location.
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps({
        "version": "1.0.0",
        "rules": [
            {"name": "test_ceiling", "type": "max_cost", "threshold": 5.0},
        ],
    }))
    return PolicyOptimizer(rules_path=rules_path, backup_dir=tmp_path / "backups")


# ---------------------------------------------------------------------------
# Shadow Mode Experiments
# ---------------------------------------------------------------------------

class TestShadowMode:
    async def test_start_experiment(self, kaizen: KaizenEngine):
        exp_id = await kaizen.start_experiment(
            control_model="claude-opus",
            experiment_model="gemini-flash",
            logic_density=3,
            description="Test substitution for trivial tasks",
        )
        assert len(exp_id) == 16
        assert exp_id in kaizen._experiments

    async def test_experiment_promotion(self, kaizen: KaizenEngine):
        """Experiment that is 97% quality and 50% cheaper should be PENDING_REVIEW."""
        exp_id = await kaizen.start_experiment("claude-opus", "gemini-flash", 3)

        # Control observations.
        for _ in range(5):
            await kaizen.record_observation(exp_id, is_control=True, quality_score=0.90, cost_usd=0.10)
        # Experiment observations — 97% quality, 50% cheaper.
        for _ in range(5):
            await kaizen.record_observation(exp_id, is_control=False, quality_score=0.873, cost_usd=0.05)

        result = await kaizen.evaluate_experiment(exp_id)
        assert result is not None
        assert result.status == ExperimentStatus.PENDING_REVIEW
        assert result.quality_retention_pct >= 95.0
        assert result.cost_savings_pct >= 30.0

    async def test_experiment_rejection_low_quality(self, kaizen: KaizenEngine):
        """Experiment with only 80% quality retention should be REJECTED."""
        exp_id = await kaizen.start_experiment("claude-opus", "gemini-flash", 3)

        for _ in range(5):
            await kaizen.record_observation(exp_id, is_control=True, quality_score=0.90, cost_usd=0.10)
        for _ in range(5):
            await kaizen.record_observation(exp_id, is_control=False, quality_score=0.70, cost_usd=0.03)

        result = await kaizen.evaluate_experiment(exp_id)
        assert result is not None
        assert result.status == ExperimentStatus.REJECTED

    async def test_experiment_rejection_insufficient_savings(self, kaizen: KaizenEngine):
        """Only 10% cheaper — should be REJECTED despite good quality."""
        exp_id = await kaizen.start_experiment("claude-sonnet", "gpt-4o-mini", 5)

        for _ in range(5):
            await kaizen.record_observation(exp_id, is_control=True, quality_score=0.85, cost_usd=0.10)
        for _ in range(5):
            await kaizen.record_observation(exp_id, is_control=False, quality_score=0.84, cost_usd=0.09)

        result = await kaizen.evaluate_experiment(exp_id)
        assert result is not None
        assert result.status == ExperimentStatus.REJECTED

    async def test_auto_promote(self, fake_redis, evolution_log):
        """When auto_promote=True, passing experiments are directly promoted."""
        kaizen = KaizenEngine(
            redis_client=fake_redis,
            evolution_log_path=evolution_log,
            auto_promote=True,
        )
        exp_id = await kaizen.start_experiment("claude-opus", "gemini-flash", 2)

        for _ in range(3):
            await kaizen.record_observation(exp_id, is_control=True, quality_score=0.95, cost_usd=0.15)
            await kaizen.record_observation(exp_id, is_control=False, quality_score=0.93, cost_usd=0.02)

        result = await kaizen.evaluate_experiment(exp_id)
        assert result.status == ExperimentStatus.PROMOTED

        # Evolution log should have the entry.
        log = kaizen.get_evolution_log()
        assert len(log) == 1
        assert log[0]["SCAMPER_TYPE"] == ScamperType.SUBSTITUTE.value


# ---------------------------------------------------------------------------
# Muda Detector
# ---------------------------------------------------------------------------

class TestMudaDetector:
    async def test_detect_latent_tasks(self, kaizen: KaizenEngine):
        """Tasks started but not completed should appear as latent."""
        await kaizen.record_task_start("bot-1", "task-abc")
        await kaizen.record_task_start("bot-2", "task-def")
        # Only complete one.
        await kaizen.record_task_complete("bot-1", "task-abc")

        report = await kaizen.detect_muda()
        assert len(report.latent_tasks) == 1
        assert report.latent_tasks[0]["agent_id"] == "bot-2"
        assert report.latent_tasks[0]["task_id"] == "task-def"

    async def test_detect_lock_bottlenecks(self, kaizen: KaizenEngine):
        """Long lock waits should be flagged as bottlenecks."""
        for i in range(10):
            await kaizen.record_lock_wait("wallet_sui", f"bot-{i}", 8.0 + i)

        report = await kaizen.detect_muda()
        assert len(report.lock_bottlenecks) == 1
        assert report.lock_bottlenecks[0]["resource"] == "wallet_sui"
        assert report.lock_bottlenecks[0]["avg_wait_seconds"] > 5.0
        assert len(report.suggested_reversals) == 1


# ---------------------------------------------------------------------------
# Commit / Rollback
# ---------------------------------------------------------------------------

class TestCommitRollback:
    async def test_commit_stages_to_log(self, kaizen: KaizenEngine, evolution_log):
        exp_id = await kaizen.start_experiment("claude-opus", "gemini-flash", 3)
        for _ in range(5):
            await kaizen.record_observation(exp_id, is_control=True, quality_score=0.90, cost_usd=0.10)
            await kaizen.record_observation(exp_id, is_control=False, quality_score=0.88, cost_usd=0.03)

        await kaizen.evaluate_experiment(exp_id)
        committed = await kaizen.commit_staged_changes()
        assert len(committed) == 1

        log = kaizen.get_evolution_log()
        assert len(log) == 1
        assert log[0]["committed"] is True

    async def test_rollback_discards_pending(self, kaizen: KaizenEngine):
        exp_id = await kaizen.start_experiment("claude-opus", "gemini-flash", 3)
        for _ in range(5):
            await kaizen.record_observation(exp_id, is_control=True, quality_score=0.90, cost_usd=0.10)
            await kaizen.record_observation(exp_id, is_control=False, quality_score=0.88, cost_usd=0.03)

        await kaizen.evaluate_experiment(exp_id)
        count = await kaizen.rollback_staged_changes()
        assert count == 1

        log = kaizen.get_evolution_log()
        assert len(log) == 0


# ---------------------------------------------------------------------------
# Policy Optimizer
# ---------------------------------------------------------------------------

class TestPolicyOptimizer:
    async def test_update_threshold(self, optimizer: PolicyOptimizer):
        result = await optimizer.update_threshold("test_ceiling", 10.0)
        assert result is True

        rules = optimizer._load_rules()
        assert rules["rules"][0]["threshold"] == 10.0

    async def test_add_and_remove_rule(self, optimizer: PolicyOptimizer):
        await optimizer.add_rule({
            "name": "temp_rule", "type": "max_cost", "threshold": 1.0,
        })
        rules = optimizer._load_rules()
        assert len(rules["rules"]) == 2

        removed = await optimizer.remove_rule("temp_rule")
        assert removed is True
        rules = optimizer._load_rules()
        assert len(rules["rules"]) == 1

    async def test_backup_created(self, optimizer: PolicyOptimizer):
        await optimizer.update_threshold("test_ceiling", 7.5)
        backups = optimizer.list_backups()
        assert len(backups) >= 1

    async def test_restore_backup(self, optimizer: PolicyOptimizer):
        # Record original.
        original = optimizer._load_rules()["rules"][0]["threshold"]
        # Modify.
        await optimizer.update_threshold("test_ceiling", 99.0)
        # Backup exists.
        backups = optimizer.list_backups()
        assert len(backups) >= 1
        # Restore.
        restored = await optimizer.restore_backup(backups[0])
        assert restored is True
        rules = optimizer._load_rules()
        assert rules["rules"][0]["threshold"] == original


# ---------------------------------------------------------------------------
# Kaizen Summary
# ---------------------------------------------------------------------------

class TestKaizenSummary:
    async def test_summary_generation(self, kaizen: KaizenEngine):
        summary = await kaizen.generate_kaizen_summary()
        assert "generated_at" in summary
        assert "experiments" in summary
        assert "staged_changes" in summary
        assert "muda_report" in summary

    async def test_telegram_formatting(self, kaizen: KaizenEngine):
        summary = await kaizen.generate_kaizen_summary()
        text = kaizen.format_telegram_summary(summary)
        assert "Kaizen Summary" in text
