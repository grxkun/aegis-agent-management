"""
PolicyOptimizer — Dynamic rules.json Rewriting.

Applies SCAMPER-driven evolution entries to the live policy rule-set.
Changes are written atomically (write to temp, then rename) to prevent
corruption if the process crashes mid-write.

Supported mutations:
    - **Substitute**: Rewrite model routing rules based on experiment results.
    - **Eliminate**: Remove rules that the Muda detector flags as dead weight.
    - **Reverse**: Invert priority or threshold values.
    - **Modify**: Adjust thresholds (cost ceilings, priority floors).
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("aegis.policy.optimizer")

_DEFAULT_RULES_PATH = Path(__file__).parent / "rules.json"


@dataclass
class PolicyOptimizer:
    """
    Reads, mutates, and atomically rewrites the policy rules.json.

    Parameters
    ----------
    rules_path : Path
        Path to the live rules.json file.
    backup_dir : Path | None
        Directory for timestamped backups before each rewrite.
    """

    rules_path: Path = field(default_factory=lambda: _DEFAULT_RULES_PATH)
    backup_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.backup_dir is None:
            self.backup_dir = self.rules_path.parent / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def apply_evolution(self, entry) -> bool:
        """
        Apply a single EvolutionEntry to rules.json.

        Dispatches based on the SCAMPER_TYPE field.
        """
        scamper_type = entry.scamper_type if hasattr(entry, "scamper_type") else entry.get("SCAMPER_TYPE", "")
        previous = entry.previous_policy if hasattr(entry, "previous_policy") else entry.get("PREVIOUS_POLICY", "")
        new = entry.new_policy if hasattr(entry, "new_policy") else entry.get("NEW_POLICY", "")

        rules = self._load_rules()
        if rules is None:
            return False

        self._backup()
        modified = False

        if scamper_type == "Substitute":
            modified = self._apply_substitute(rules, previous, new)
        elif scamper_type == "Eliminate":
            modified = self._apply_eliminate(rules, previous)
        elif scamper_type == "Reverse":
            modified = self._apply_reverse(rules, previous, new)
        elif scamper_type == "Modify":
            modified = self._apply_modify(rules, previous, new)
        else:
            logger.warning("Unsupported SCAMPER type: %s", scamper_type)
            return False

        if modified:
            self._save_rules(rules)
            logger.info(
                "POLICY UPDATED [%s]: '%s' -> '%s'",
                scamper_type, previous, new,
            )
        return modified

    async def add_rule(self, rule: dict[str, Any]) -> None:
        """Add a new rule to the rule-set."""
        rules = self._load_rules() or {"version": "1.0.0", "rules": []}
        self._backup()
        rules["rules"].append(rule)
        self._save_rules(rules)
        logger.info("Rule added: %s", rule.get("name", "unnamed"))

    async def remove_rule(self, rule_name: str) -> bool:
        """Remove a rule by name."""
        rules = self._load_rules()
        if not rules:
            return False
        self._backup()
        original_count = len(rules["rules"])
        rules["rules"] = [r for r in rules["rules"] if r.get("name") != rule_name]
        if len(rules["rules"]) < original_count:
            self._save_rules(rules)
            logger.info("Rule removed: %s", rule_name)
            return True
        return False

    async def update_threshold(self, rule_name: str, new_threshold: float) -> bool:
        """Update the threshold of a max_cost rule."""
        rules = self._load_rules()
        if not rules:
            return False
        self._backup()
        for rule in rules["rules"]:
            if rule.get("name") == rule_name and rule.get("type") == "max_cost":
                rule["threshold"] = new_threshold
                self._save_rules(rules)
                logger.info("Threshold updated: %s -> %.2f", rule_name, new_threshold)
                return True
        return False

    def list_backups(self) -> list[str]:
        """Return sorted list of backup filenames."""
        return sorted(f.name for f in self.backup_dir.glob("rules_*.json"))

    async def restore_backup(self, backup_name: str) -> bool:
        """Restore a specific backup as the active rules.json."""
        backup_path = self.backup_dir / backup_name
        if not backup_path.exists():
            logger.error("Backup not found: %s", backup_name)
            return False
        shutil.copy2(backup_path, self.rules_path)
        logger.info("POLICY RESTORED from backup: %s", backup_name)
        return True

    # ------------------------------------------------------------------
    # SCAMPER mutation strategies
    # ------------------------------------------------------------------

    def _apply_substitute(
        self, rules: dict, previous: str, new: str
    ) -> bool:
        """
        Add a model-routing override rule.

        Example: "Use gemini-flash for logic_density=3" substitutes
        whatever was previously used for that density level.
        """
        # Parse model and density from the policy strings.
        new_model = self._extract_model(new)
        density = self._extract_density(new)

        if not new_model:
            logger.warning("Could not parse model from new_policy: %s", new)
            return False

        # Add a routing override rule.
        override_rule = {
            "name": f"kaizen_substitute_{new_model}_{density or 'any'}",
            "type": "model_override",
            "description": f"Auto-optimised: {previous} -> {new}",
            "model": new_model,
            "logic_density": density,
            "generated_by": "kaizen_engine",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Remove any previous override for the same density.
        rules["rules"] = [
            r for r in rules["rules"]
            if not (
                r.get("type") == "model_override"
                and r.get("logic_density") == density
                and r.get("generated_by") == "kaizen_engine"
            )
        ]
        rules["rules"].append(override_rule)
        return True

    def _apply_eliminate(self, rules: dict, target: str) -> bool:
        """Remove a rule identified by the Muda detector as dead weight."""
        original = len(rules["rules"])
        rules["rules"] = [
            r for r in rules["rules"]
            if r.get("name") != target and r.get("description", "") != target
        ]
        return len(rules["rules"]) < original

    def _apply_reverse(
        self, rules: dict, previous: str, new: str
    ) -> bool:
        """Reverse a threshold or priority value in an existing rule."""
        # Generic: try to find a rule matching the previous policy description
        # and update it.
        for rule in rules["rules"]:
            if previous in (rule.get("name", ""), rule.get("description", "")):
                if "min_priority" in rule:
                    # Invert priority (e.g. 7 -> 3).
                    old_val = rule["min_priority"]
                    rule["min_priority"] = max(1, 10 - old_val)
                    return True
                if "threshold" in rule:
                    old_val = rule["threshold"]
                    # Parse new threshold from new_policy if possible.
                    new_threshold = self._extract_number(new)
                    if new_threshold is not None:
                        rule["threshold"] = new_threshold
                        return True
        return False

    def _apply_modify(
        self, rules: dict, previous: str, new: str
    ) -> bool:
        """Modify a numeric threshold in a matching rule."""
        new_val = self._extract_number(new)
        if new_val is None:
            return False
        for rule in rules["rules"]:
            if previous in (rule.get("name", ""), rule.get("description", "")):
                if "threshold" in rule:
                    rule["threshold"] = new_val
                    return True
                if "min_priority" in rule:
                    rule["min_priority"] = int(new_val)
                    return True
        return False

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _load_rules(self) -> Optional[dict]:
        try:
            with open(self.rules_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            logger.error("Failed to load rules: %s", exc)
            return None

    def _save_rules(self, data: dict) -> None:
        """Atomic write: temp file then rename."""
        tmp_path = self.rules_path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(self.rules_path)

    def _backup(self) -> None:
        """Create a timestamped backup of the current rules.json."""
        if not self.rules_path.exists():
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = self.backup_dir / f"rules_{ts}.json"
        shutil.copy2(self.rules_path, backup)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_model(policy_text: str) -> Optional[str]:
        """Extract a model alias from a policy string like 'Use gemini-flash for ...'"""
        known_models = [
            "claude-opus", "claude-sonnet", "claude-haiku",
            "gpt-4o", "gpt-4o-mini", "o3",
            "gemini-flash", "gemini-pro",
        ]
        lower = policy_text.lower()
        for model in known_models:
            if model in lower:
                return model
        return None

    @staticmethod
    def _extract_density(policy_text: str) -> Optional[int]:
        """Extract logic_density=N from a policy string."""
        import re
        match = re.search(r"logic_density\s*=\s*(\d+)", policy_text)
        return int(match.group(1)) if match else None

    @staticmethod
    def _extract_number(text: str) -> Optional[float]:
        """Extract the first numeric value from a string."""
        import re
        match = re.search(r"[\d.]+", text)
        return float(match.group()) if match else None
