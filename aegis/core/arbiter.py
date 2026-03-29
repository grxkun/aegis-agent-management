"""
ModelArbiter — Intelligence Routing via Task-Logic Density.

Zero-Waste Principle:
    An agent requesting "summarise this paragraph" should never be routed to
    ``claude-opus`` when ``gemini-flash`` can handle it at 1/50th the cost.

The Arbiter maps a *Logic Density* score (1–10) to a tier of models, then
selects the cheapest model within that tier.  The mapping is configurable
at runtime so operators can adjust as pricing changes.

Logic Density Guidelines (for callers):
    1–3   Trivial: summarisation, reformatting, simple extraction.
    4–5   Moderate: multi-step reasoning, classification with nuance.
    6–7   Complex: code generation, planning, structured analysis.
    8–10  Expert: novel research synthesis, adversarial reasoning, agentic
          tool-use chains requiring deep context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("aegis.arbiter")


@dataclass(frozen=True)
class ModelSpec:
    """Describes a model available for routing."""
    alias: str
    provider: str
    cost_per_1k_tokens: float  # USD
    max_context: int = 128_000
    supports_tools: bool = True


# ---------------------------------------------------------------------------
# Default model catalogue — kept as module-level data so it can be patched
# in tests or overridden via config.
# ---------------------------------------------------------------------------

DEFAULT_MODELS: list[ModelSpec] = [
    # -- Tier 1: Trivial (Logic 1–3) --
    ModelSpec("gemini-flash",       "google",    0.00010, 1_000_000),
    ModelSpec("claude-haiku",       "anthropic", 0.00025, 200_000),
    # -- Tier 2: Moderate (Logic 4–5) --
    ModelSpec("gpt-4o-mini",        "openai",    0.00015, 128_000),
    ModelSpec("claude-sonnet",      "anthropic", 0.00300, 200_000),
    # -- Tier 3: Complex (Logic 6–7) --
    ModelSpec("gpt-4o",             "openai",    0.00500, 128_000),
    ModelSpec("gemini-pro",         "google",    0.00350, 2_000_000),
    # -- Tier 4: Expert (Logic 8–10) --
    ModelSpec("claude-opus",        "anthropic", 0.01500, 200_000),
    ModelSpec("o3",                 "openai",    0.01000, 200_000),
]

# Maps a logic-density range to the *minimum tier* of models that satisfy it.
DEFAULT_TIER_MAP: dict[range, int] = {
    range(1, 4): 1,
    range(4, 6): 2,
    range(6, 8): 3,
    range(8, 11): 4,
}


@dataclass
class ModelArbiter:
    """
    Recommends the cheapest model that satisfies a task's complexity.

    Parameters
    ----------
    models : list[ModelSpec]
        Available model catalogue.
    tier_map : dict[range, int]
        Mapping from logic-density ranges to minimum tier numbers.
    """

    models: list[ModelSpec] = field(default_factory=lambda: list(DEFAULT_MODELS))
    tier_map: dict[range, int] = field(default_factory=lambda: dict(DEFAULT_TIER_MAP))

    # Tier boundaries (inclusive) for each tier number.
    _TIER_RANGES: dict[int, tuple[int, int]] = field(
        init=False,
        default_factory=lambda: {1: (0, 1), 2: (2, 3), 3: (4, 5), 4: (6, 7)},
    )

    def recommend(
        self,
        logic_density: int,
        require_tools: bool = False,
        max_cost: Optional[float] = None,
    ) -> Optional[ModelSpec]:
        """
        Return the cheapest eligible model for the given logic density.

        Parameters
        ----------
        logic_density : int
            Score from 1 (trivial) to 10 (expert).
        require_tools : bool
            If True, only models with ``supports_tools=True`` are eligible.
        max_cost : float | None
            Optional hard ceiling on cost_per_1k_tokens.

        Returns
        -------
        ModelSpec | None
            The recommended model, or None if nothing qualifies.
        """
        if not 1 <= logic_density <= 10:
            raise ValueError(f"logic_density must be 1–10, got {logic_density}")

        min_tier = self._resolve_tier(logic_density)
        eligible = self._eligible_models(min_tier, require_tools, max_cost)

        if not eligible:
            logger.warning(
                "No eligible model for logic_density=%d require_tools=%s max_cost=%s",
                logic_density, require_tools, max_cost,
            )
            return None

        # Sort by cost ascending — cheapest first (Zero-Waste).
        best = min(eligible, key=lambda m: m.cost_per_1k_tokens)
        logger.info(
            "Arbiter recommends %s for logic_density=%d (cost=$%.5f/1k)",
            best.alias, logic_density, best.cost_per_1k_tokens,
        )
        return best

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_tier(self, logic_density: int) -> int:
        for density_range, tier in self.tier_map.items():
            if logic_density in density_range:
                return tier
        # Fallback: highest tier for unrecognised densities.
        return max(self.tier_map.values())

    def _eligible_models(
        self,
        min_tier: int,
        require_tools: bool,
        max_cost: Optional[float],
    ) -> list[ModelSpec]:
        """Return models whose tier >= min_tier, respecting filters."""
        # Models are stored in tier order in the catalogue, so we slice
        # from the appropriate starting index.
        tier_start_idx = self._TIER_RANGES.get(min_tier, (0, 0))[0]
        candidates = self.models[tier_start_idx:]

        if require_tools:
            candidates = [m for m in candidates if m.supports_tools]
        if max_cost is not None:
            candidates = [m for m in candidates if m.cost_per_1k_tokens <= max_cost]
        return candidates
