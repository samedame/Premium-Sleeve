"""Monitor open positions for profit target, stop and time exit.

Runs whether or not the gate is open: the gate blocks new entries, it
never traps an open position.
"""
from __future__ import annotations

import datetime as dt
import sys

from . import alerts, data, risk, state
from .config import load


def _current_debit(cfg, pos: state.Position) -> tuple[float | None, str | None]:
    """Per-share cost to buy the spread back, at mid plus a haircut.

    Returns (debit, error). A non-None error means the position could
    not be priced and must be flagged, never quietly left open.
    """
    expiry = pos.expiry_date
    try:
        short_q = data.quote_for(cfg.underlying, expiry, pos.short_strike)
        long_q = data.quote_for(cfg.underlying, expiry, pos.long_strike)
    except data.DataError as exc:
        return None, f"market data unavailable: {exc}"
    if short_q is None or long_q is None:
        return None, "strike no longer listed"
    if short_q.mid <= 0 or long_q.mid <= 0:
        return None, "no two-sided quote on one leg"
    slippage = cfg.fill_haircut * (short_q.width + long_q.width)
    if slippage == float("inf"):
        return None, "leg quoted one-sided"
    return max(0.0, (short_q.mid - long_q.mid) + slippage), None


def main() -> int:
    cfg = load()
    today = dt.datetime.now(dt.timezone.utc).date()
    positions = state.load_positions(cfg.positions_path)
    equity = state.load_equity(cfg.equity_path)
    open_pos = state.open_positions(positions)

    if equity.halted:
        equity.sessions_since_halt += 1
        state.save_equity(cfg.equity_path, equity)

    if not open_pos:
        print("no open positions")
        return 0

    closed_any = False
    lines = []
    blind: list[str] = []

    for pos in open_pos:
        debit, err = _current_debit(cfg, pos)
        if err is not None:
            blind.append(
                f"· {pos.id} {pos.short_strike:g}/{pos.long_strike:g} "
                f"exp {pos.expiry} ({pos.dte(today)}d): {err}"
            )
            continue

        should_exit, reason = risk.exit_check(pos, debit, today, cfg)
        captured = pos.credit - debit
        if not should_exit:
            lines.append(
                f"· {pos.id} {pos.short_strike:g}/{pos.long_strike:g} "
                f"{pos.dte(today)}d: debit {debit:.2f} vs credit {pos.credit:.2f} "
                f"({captured / pos.credit:+.0%})" if pos.credit else f"· {pos.id}: open"
            )
            continue

        pnl_per_contract = captured * 100.0
        pos.status = "closed"
        pos.closed = today.isoformat()
        pos.exit_debit = round(debit, 4)
        pos.exit_reason = reason
        pos.realized_per_contract = round(pnl_per_contract, 2)

        total = pnl_per_contract * (pos.contracts or 0)
        equity = state.record_close(equity, total, today, f"{pos.id}: {reason}")
        alerts.exit_alert(cfg.discord_webhook, pos, debit, reason, pnl_per_contract, cfg.dry_run)
        closed_any = True

    dd = state.drawdown(equity)
    if not equity.halted and dd >= cfg.kill_drawdown_pct:
        equity.halted = True
        equity.halted_on = today.isoformat()
        equity.sessions_since_halt = 0
        alerts.status_alert(
            cfg.discord_webhook, "KILL SWITCH TRIPPED",
            [f"drawdown {dd:.1%} at or beyond {cfg.kill_drawdown_pct:.0%}",
             f"no new entries for {cfg.kill_restart_sessions} sessions, "
             f"then only with VIX/VIX3M below {cfg.kill_restart_max_ratio:.2f}",
             "open positions continue to be monitored"],
            False, cfg.dry_run)

    if not cfg.dry_run:
        state.save_positions(cfg.positions_path, positions)
        state.save_equity(cfg.equity_path, equity)

    if blind:
        alerts.status_alert(
            cfg.discord_webhook, "UNPRICED POSITIONS",
            ["could not price the following open position(s); exit rules were "
             "NOT evaluated for them -- check manually:"] + blind,
            False, cfg.dry_run)
    if lines and not closed_any:
        alerts.status_alert(cfg.discord_webhook, "MONITOR", lines, True, cfg.dry_run)
    return 1 if blind else 0


if __name__ == "__main__":
    sys.exit(main())
