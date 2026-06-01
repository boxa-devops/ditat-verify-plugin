"""Load + merge verification rules from rules.yaml.

The built-in DEFAULTS below mirror the historical hardcoded thresholds, so
diff.py works with no rules file at all (tests rely on this). A YAML file is
shallow-merged over the defaults — omit any key to keep its default.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("ditat")

DEFAULTS: dict[str, Any] = {
    "weight_ditat_rc": {"critical_pct": 5.0, "warn_pct": 1.0},
    "bol_rc_overage": {"weight_threshold_pct": 10.0, "pieces_threshold_pct": 10.0},
    "money": {"critical_abs": 1.0, "critical_pct": 1.0},
    "date": {"critical_days": 1},
    "string_mismatch_severity": "warn",
    "accessorial": {
        "detention_rate": 50.0,
        "detention_free_hrs": 2.0,
        "detention_max_hrs": 5.0,
        "layover_rate": 250.0,
        "layover_threshold_hrs": 5.0,
        "severities": {
            "detention_rate": "critical",
            "detention_free_hrs": "critical",
            "detention_max_hrs": "warn",
            "layover_rate": "critical",
            "layover_threshold_hrs": "warn",
        },
    },
    "rc_missing_ok_customers": ["amazon"],
}


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge `over` onto a deep copy of `base` (dicts merge, others
    replace). The result shares no mutable references with either input.
    """
    out = {k: copy.deepcopy(v) for k, v in base.items()}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_rules() -> dict:
    """A standalone copy of the built-in defaults."""
    return _deep_merge(DEFAULTS, {})


def _default_rules_path() -> Path | None:
    """Locate rules.yaml: env override → project dir → scripts/ alongside package."""
    explicit = os.getenv("DITAT_RULES_PATH")
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    project = os.getenv("CLAUDE_PROJECT_DIR")
    candidates = []
    if project:
        candidates.append(Path(project) / "rules.yaml")
        candidates.append(Path(project) / "scripts" / "rules.yaml")
    candidates.append(Path.cwd() / "rules.yaml")
    candidates.append(Path(__file__).resolve().parent.parent / "rules.yaml")
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_rules(path: str | os.PathLike | None = None) -> dict:
    """Return DEFAULTS merged with rules.yaml. Missing file → pure defaults.

    pyyaml is imported lazily so the diff layer still works (on defaults) when
    PyYAML isn't installed.
    """
    rules_path = Path(path) if path else _default_rules_path()
    if not rules_path or not rules_path.is_file():
        return default_rules()
    try:
        import yaml  # lazy — keep PyYAML optional
    except ImportError:
        log.warning("PyYAML not installed — using built-in default rules. "
                    "Run: pip install -r scripts/requirements.txt")
        return default_rules()
    try:
        loaded = yaml.safe_load(rules_path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — a broken file shouldn't crash a run
        log.warning("Failed to parse %s (%s) — using default rules.", rules_path, e)
        return default_rules()
    if not isinstance(loaded, dict):
        log.warning("%s is not a mapping — using default rules.", rules_path)
        return default_rules()
    log.info("Loaded rules from %s", rules_path)
    return _deep_merge(DEFAULTS, loaded)
