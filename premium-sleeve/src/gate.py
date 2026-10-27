"""The vol-regime gate: the sleeve's only on/off switch.

Backwardation (VIX/VIX3M at or above threshold) blocks new entries. It
never force-closes an open position -- exits are governed by the
position's own profit target, stop and time rule.

Sam's existing gate lives in local `research 3` and is not importable
from GitHub Actions. Until it is published, this reimplements the same
single rule. To hand control to that gate instead, have it write
state/gate_override.json: {"allow_entries": false, "reason": "..."}.
An override file always wins.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .config import Config


@dataclass
class GateDecision:
    allow_entries: bool
    reason: str
    ratio: float | None = None
    source: str = "internal"


def _override(path: str) -> GateDecision | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if "allow_entries" not in raw:
        return None
    return GateDecision(
        allow_entries=bool(raw["allow_entries"]),
        reason=str(raw.get("reason", "external gate override")),
        ratio=raw.get("ratio"),
        source="external",
    )


def evaluate(ratio: float, cfg: Config) -> GateDecision:
    ext = _override(cfg.gate_external_path)
    if ext is not None:
        return ext
    if ratio >= cfg.gate_ratio_threshold:
        return GateDecision(
            False,
            f"backwardation: VIX/VIX3M {ratio:.3f} >= {cfg.gate_ratio_threshold:.2f}",
            ratio,
        )
    return GateDecision(
        True, f"contango: VIX/VIX3M {ratio:.3f} < {cfg.gate_ratio_threshold:.2f}", ratio
    )
