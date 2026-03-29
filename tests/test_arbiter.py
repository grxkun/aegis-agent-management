"""
Tests for ModelArbiter — intelligence routing.
"""

from __future__ import annotations

import pytest

from aegis.core.arbiter import ModelArbiter


@pytest.fixture
def arbiter():
    return ModelArbiter()


class TestModelRouting:
    def test_trivial_task_gets_cheapest_model(self, arbiter: ModelArbiter):
        spec = arbiter.recommend(logic_density=1)
        assert spec is not None
        assert spec.alias == "gemini-flash"

    def test_expert_task_gets_high_tier(self, arbiter: ModelArbiter):
        spec = arbiter.recommend(logic_density=10)
        assert spec is not None
        assert spec.alias in ("claude-opus", "o3")

    def test_moderate_task_routing(self, arbiter: ModelArbiter):
        spec = arbiter.recommend(logic_density=4)
        assert spec is not None
        # Should be tier 2+ but still cheapest eligible.
        assert spec.cost_per_1k_tokens <= 0.01

    def test_invalid_density_raises(self, arbiter: ModelArbiter):
        with pytest.raises(ValueError):
            arbiter.recommend(logic_density=0)
        with pytest.raises(ValueError):
            arbiter.recommend(logic_density=11)

    def test_max_cost_filter(self, arbiter: ModelArbiter):
        spec = arbiter.recommend(logic_density=8, max_cost=0.005)
        # At density 8, cheapest tier-4 model costs more than $0.005.
        # Should fall back to whatever fits, or None.
        if spec is not None:
            assert spec.cost_per_1k_tokens <= 0.005

    def test_no_model_returns_none(self, arbiter: ModelArbiter):
        result = arbiter.recommend(logic_density=10, max_cost=0.0001)
        assert result is None
