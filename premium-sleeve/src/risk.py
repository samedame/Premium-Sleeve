"""The risk layer: the point of consolidating four bots into one.

Four separate bots each sold SPY/QQQ premium with no shared limit, so
their vega stacked invisibly. Every check here applies to the book as a
whole, which is what the separate bots could not do.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .config import Config
from .selector import Candidate
from .state import Equity, Position, drawdown


@dataclass
class Verdict:
    allowed: bool
    reasons: list[str]
    contracts: int | None = None
    unsized: bool = False


@dataclass
class BookGreeks:
    vega: float      # dollars per vol point; negative = short vega
    delta: float
    risk: float      # total defined risk in dollars


def book_greeks(positions: list[Position]) -> BookGreeks:
    v = d = risk = 0.0
    for p in positions:
        n = p.contracts or 0
        v += p.net_vega_per_contract * n
        d += p.net_delta_per_contract * n
        risk += p.max_loss_per_contract * n
    return BookGreeks(vega=v, delta=d, risk=risk)


def kill_switch(equity: Equity, cfg: Config, ratio: float | None) -> tuple[bool, str]:
    """Return (halted, reason). Handles both tripping and restarting."""
    dd = drawdown(equity)

    if equity.halted:
        if equity.sessions_since_halt < cfg.kill_restart_sessions:
            left = cfg.kill_restart_sessions - equity.sessions_since_halt
            return True, f"halted: {left} of {cfg.kill_restart_sessions} sessions remaining"
        if ratio is None or ratio >= cfg.kill_restart_max_ratio:
            shown = f"{ratio:.3f}" if ratio is not None else "unknown"
            return True, (
                f"halted: waiting for VIX/VIX3M below {cfg.kill_restart_max_ratio:.2f} "
                f"(now {shown})"
            )
        return False, "restart conditions met: sessions served and term structure in contango"

    if dd >= cfg.kill_drawdown_pct:
        return True, f"kill switch: drawdown {dd:.1%} at or beyond {cfg.kill_drawdown_pct:.0%}"
    return False, f"drawdown {dd:.1%} within {cfg.kill_drawdown_pct:.0%} limit"


def size_position(cand: Candidate, open_pos: list[Position], cfg: Config) -> Verdict:
    """Decide whether this candidate may be opened, and for how many.

    With no ACCOUNT_CAPITAL_FOR_SIZING the structural checks still run
    and the trade is reported UNSIZED: shown, but with no contract count.
    """
    reasons: list[str] = []

    if len(open_pos) >= cfg.max_concurrent:
        return Verdict(False, [f"at max concurrent positions ({cfg.max_concurrent})"])

    for p in open_pos:
        gap = abs((cand.expiry - p.expiry_date).days)
        if gap < cfg.stagger_min_days:
            return Verdict(
                False,
                [f"expiry {cand.expiry} is {gap}d from open position {p.id} "
                 f"(minimum stagger {cfg.stagger_min_days}d)"],
            )

    if cfg.account_capital is None:
        return Verdict(
            True,
            ["UNSIZED: ACCOUNT_CAPITAL_FOR_SIZING not set, no contract count issued"],
            contracts=None,
            unsized=True,
        )

    capital = cfg.account_capital
    if capital < cfg.capital_floor_warn:
        reasons.append(
            f"capital ${capital:,.0f} is below the ${cfg.capital_floor_warn:,.0f} "
            "floor for a multi-position book"
        )

    per_pos_cap = capital * cfg.max_risk_per_position_pct
    by_position = int(per_pos_cap // cand.max_loss_per_contract)

    current = book_greeks(open_pos)
    book_cap = capital * cfg.max_book_risk_pct
    remaining_risk = book_cap - current.risk
    by_book = int(remaining_risk // cand.max_loss_per_contract) if remaining_risk > 0 else 0

    vega_cap = capital * cfg.max_book_vega_pct
    remaining_vega = vega_cap - abs(current.vega)
    per_contract_vega = abs(cand.net_vega_per_contract)
    by_vega = (
        int(remaining_vega // per_contract_vega)
        if remaining_vega > 0 and per_contract_vega > 0
        else 0
    )

    contracts = max(0, min(by_position, by_book, by_vega))

    if contracts == 0:
        binding = min(
            ("per-position risk cap", by_position),
            ("book risk cap", by_book),
            ("book vega budget", by_vega),
            key=lambda kv: kv[1],
        )[0]
        return Verdict(False, reasons + [f"sizes to zero contracts: {binding} is binding"])

    limits = {"per-position risk": by_position, "book risk": by_book, "book vega": by_vega}
    binding = min(limits, key=limits.get)
    reasons.append(
        f"{contracts} contract(s); binding limit is {binding} "
        f"(risk ${cand.max_loss_per_contract * contracts:,.0f}, "
        f"vega {cand.net_vega_per_contract * contracts:+.1f}/pt)"
    )
    return Verdict(True, reasons, contracts=contracts)


def exit_check(
    pos: Position, current_debit: float, today: dt.date, cfg: Config
) -> tuple[bool, str | None]:
    """Should this open position be closed now?

    current_debit is the per-share cost to buy the spread back.
    """
    captured = pos.credit - current_debit
    if pos.credit > 0 and captured >= cfg.profit_target_pct * pos.credit:
        return True, (
            f"profit target: captured {captured:.2f} of {pos.credit:.2f} credit "
            f"({captured / pos.credit:.0%})"
        )
    if current_debit >= cfg.stop_loss_multiple * pos.credit:
        return True, (
            f"stop loss: debit {current_debit:.2f} at or beyond "
            f"{cfg.stop_loss_multiple:.0f}x the {pos.credit:.2f} credit"
        )
    dte = pos.dte(today)
    if dte <= cfg.time_exit_dte:
        return True, f"time exit: {dte} DTE at or below {cfg.time_exit_dte}"
    return False, None
