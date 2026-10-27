"""Position and equity state, persisted as JSON in the repo.

Committing state back to the repo is deliberate: the push webhook is
what carries it to BotDeck. There is no POST endpoint.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from dataclasses import asdict, dataclass, field


@dataclass
class Position:
    id: str
    opened: str              # ISO date
    expiry: str              # ISO date
    short_strike: float
    long_strike: float
    contracts: int | None    # None in UNSIZED mode
    credit: float            # per share, at entry
    max_loss_per_contract: float
    net_vega_per_contract: float
    net_delta_per_contract: float
    status: str = "open"     # open | closed
    closed: str | None = None
    exit_debit: float | None = None
    exit_reason: str | None = None
    realized_per_contract: float | None = None

    @property
    def expiry_date(self) -> dt.date:
        return dt.date.fromisoformat(self.expiry)

    def dte(self, today: dt.date) -> int:
        return (self.expiry_date - today).days


@dataclass
class Equity:
    peak: float = 0.0
    current: float = 0.0
    halted: bool = False
    halted_on: str | None = None
    sessions_since_halt: int = 0
    history: list[dict] = field(default_factory=list)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def _read(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path) as fh:
        text = fh.read().strip()
    return json.loads(text) if text else default


def _write(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_positions(path: str) -> list[Position]:
    return [Position(**row) for row in _read(path, [])]


def save_positions(path: str, positions: list[Position]) -> None:
    _write(path, [asdict(p) for p in positions])


def load_equity(path: str) -> Equity:
    raw = _read(path, None)
    return Equity(**raw) if raw else Equity()


def save_equity(path: str, equity: Equity) -> None:
    _write(path, asdict(equity))


def open_positions(positions: list[Position]) -> list[Position]:
    return [p for p in positions if p.status == "open"]


def record_close(equity: Equity, pnl: float, when: dt.date, note: str) -> Equity:
    """Apply a realized P/L and update the peak used by the kill switch."""
    equity.current += pnl
    equity.peak = max(equity.peak, equity.current)
    equity.history.append({"date": when.isoformat(), "pnl": round(pnl, 2),
                           "equity": round(equity.current, 2), "note": note})
    return equity


def drawdown(equity: Equity) -> float:
    """Fractional drawdown from peak. 0.0 when at or above peak."""
    if equity.peak <= 0:
        return 0.0
    return max(0.0, (equity.peak - equity.current) / equity.peak)
