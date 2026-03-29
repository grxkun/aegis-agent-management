"""
PolicyEngine — Built-in Quality (BIQ) Guardrails.

Every agent request passes through the PolicyEngine before resources are
allocated.  Rules are loaded from a JSON definition file and evaluated
sequentially.  The first rule that matches determines the verdict.

Rule Types:
    - ``max_cost``: Reject if estimated_cost exceeds a threshold.
    - ``blocked_intent``: Reject if task_intent matches a forbidden pattern.
    - ``priority_floor``: Reject if priority is below a minimum for certain
      intent categories.
    - ``rate_limit``: (Future) Reject if request frequency exceeds a window.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("aegis.policy")

_DEFAULT_RULES_PATH = Path(__file__).parent / "rules.json"


@dataclass(frozen=True)
class PolicyVerdict:
    """Result of evaluating a request against the policy rule-set."""
    status: str  # "ALLOWED" | "DENIED"
    reason: str
    matched_rule: Optional[str] = None


@dataclass
class PolicyEngine:
    """
    Evaluate agent requests against a configurable rule-set.

    Parameters
    ----------
    rules_path : Path | None
        Path to the JSON rules file.  Uses the bundled ``rules.json`` if
        omitted.
    """

    rules_path: Path = field(default_factory=lambda: _DEFAULT_RULES_PATH)
    _rules: list[dict[str, Any]] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._load_rules()

    def evaluate(
        self,
        agent_id: str,
        task_intent: str,
        estimated_cost: float,
        priority: int,
    ) -> PolicyVerdict:
        """
        Run the request through every rule.  First match wins.

        Returns ``ALLOWED`` if no rule blocks the request.
        """
        for rule in self._rules:
            verdict = self._apply_rule(rule, agent_id, task_intent, estimated_cost, priority)
            if verdict is not None:
                return verdict

        return PolicyVerdict(status="ALLOWED", reason="No policy violation detected.")

    def reload(self) -> None:
        """Hot-reload rules from disk without restarting."""
        self._load_rules()
        logger.info("Policy rules reloaded from %s", self.rules_path)

    # ------------------------------------------------------------------
    # Rule evaluation
    # ------------------------------------------------------------------

    def _apply_rule(
        self,
        rule: dict[str, Any],
        agent_id: str,
        task_intent: str,
        estimated_cost: float,
        priority: int,
    ) -> Optional[PolicyVerdict]:
        """Evaluate a single rule.  Return a verdict if it matches, else None."""
        rule_type = rule.get("type")
        rule_name = rule.get("name", "unnamed")

        if rule_type == "max_cost":
            threshold = rule.get("threshold", float("inf"))
            if estimated_cost > threshold:
                return PolicyVerdict(
                    status="DENIED",
                    reason=f"Estimated cost ${estimated_cost:.4f} exceeds policy "
                           f"ceiling ${threshold:.4f}.",
                    matched_rule=rule_name,
                )

        elif rule_type == "blocked_intent":
            patterns = rule.get("patterns", [])
            for pattern in patterns:
                if re.search(pattern, task_intent, re.IGNORECASE):
                    return PolicyVerdict(
                        status="DENIED",
                        reason=f"Task intent matches blocked pattern '{pattern}'.",
                        matched_rule=rule_name,
                    )

        elif rule_type == "priority_floor":
            floor = rule.get("min_priority", 1)
            intent_match = rule.get("intent_pattern")
            if intent_match and re.search(intent_match, task_intent, re.IGNORECASE):
                if priority < floor:
                    return PolicyVerdict(
                        status="DENIED",
                        reason=f"Priority {priority} is below the floor of {floor} "
                               f"for this intent category.",
                        matched_rule=rule_name,
                    )

        elif rule_type == "agent_blocklist":
            blocked = rule.get("agents", [])
            if agent_id in blocked:
                return PolicyVerdict(
                    status="DENIED",
                    reason=f"Agent '{agent_id}' is on the blocklist.",
                    matched_rule=rule_name,
                )

        return None

    # ------------------------------------------------------------------
    # Rule loading
    # ------------------------------------------------------------------

    def _load_rules(self) -> None:
        try:
            with open(self.rules_path) as fh:
                data = json.load(fh)
            self._rules = data.get("rules", [])
            logger.info("Loaded %d policy rules from %s", len(self._rules), self.rules_path)
        except FileNotFoundError:
            logger.warning("Rules file not found at %s — running with empty policy.", self.rules_path)
            self._rules = []
        except json.JSONDecodeError as exc:
            logger.error("Malformed rules JSON at %s: %s", self.rules_path, exc)
            self._rules = []
