"""Entry scan: at most one new spread per run."""
from __future__ import annotations

import datetime as dt
import sys

from . import alerts, data, gate, risk, state
from .config import load
from .selector import build_candidates


def _leg_quotes(chains, cand):
    """The raw OptionQuotes (with source and age) behind a candidate's two legs."""
    out = []
    for ch in chains:
        if ch.expiry == cand.expiry:
            out += [q for q in ch.puts if q.strike in (cand.short.strike, cand.long.strike)]
    return out


def main() -> int:
    cfg = load()
    today = dt.datetime.now(dt.timezone.utc).date()
    positions = state.load_positions(cfg.positions_path)
    equity = state.load_equity(cfg.equity_path)
    open_pos = state.open_positions(positions)

    try:
        vix, vix3m, ratio = data.vix_ratio()
    except data.DataError as exc:
        alerts.status_alert(cfg.discord_webhook, "DATA ERROR",
                            [f"regime data unavailable: {exc}", "no entry attempted"],
                            False, cfg.dry_run)
        return 1

    halted, kill_reason = risk.kill_switch(equity, cfg, ratio)
    if halted:
        alerts.status_alert(cfg.discord_webhook, "HALTED", [kill_reason], False, cfg.dry_run)
        return 0
    if equity.halted:
        equity.halted = False
        equity.halted_on = None
        equity.sessions_since_halt = 0
        state.save_equity(cfg.equity_path, equity)
        alerts.status_alert(cfg.discord_webhook, "RESTARTED", [kill_reason], True, cfg.dry_run)

    decision = gate.evaluate(ratio, cfg)
    if not decision.allow_entries:
        alerts.status_alert(cfg.discord_webhook, "GATE CLOSED",
                            [decision.reason, f"VIX {vix:.2f} · VIX3M {vix3m:.2f}",
                             f"{len(open_pos)} position(s) left open; exits still run"],
                            True, cfg.dry_run)
        return 0

    try:
        spot_px = data.spot(cfg.underlying)
        chains = data.put_chains(cfg.underlying, cfg.dte_min, cfg.dte_max)
    except data.DataError as exc:
        alerts.status_alert(cfg.discord_webhook, "DATA ERROR", [str(exc)], False, cfg.dry_run)
        return 1

    candidates, rejections = build_candidates(chains, spot_px, cfg)
    if not candidates:
        lines = [f"no candidate passed the screen ({cfg.underlying} {spot_px:.2f})"]
        lines += [f"· {r.expiry}: {r.reason}" for r in rejections]
        alerts.status_alert(cfg.discord_webhook, "NO ENTRY", lines, True, cfg.dry_run)
        return 0

    for cand in candidates:
        verdict = risk.size_position(cand, open_pos, cfg)
        if not verdict.allowed:
            continue

        # Data-quality gate. The strike choice above is unchanged; this only decides whether the quotes behind it are
        # good enough to send an ENTRY alert. If not, a NOTICE goes out instead and nothing is opened.
        leg_quotes = _leg_quotes(chains, cand)
        problems = [p for p in (data.quote_problem(q) for q in leg_quotes) if p]
        if problems:
            alerts.status_alert(cfg.discord_webhook, "NOTICE: ENTRY NOT ALERTED",
                                [f"a candidate {cand.expiry} {cand.short.strike:g}/{cand.long.strike:g} was found, "
                                 "but its quotes are not usable, so no entry alert was sent:"]
                                + [f"· {p}" for p in problems]
                                + [f"data: {data.data_label(leg_quotes)}"],
                                False, cfg.dry_run)
            return 0
        label = data.data_label(leg_quotes)

        projected = list(open_pos)
        pos = state.Position(
            id=state.new_id(),
            opened=today.isoformat(),
            expiry=cand.expiry.isoformat(),
            short_strike=cand.short.strike,
            long_strike=cand.long.strike,
            contracts=verdict.contracts,
            credit=round(cand.credit, 4),
            max_loss_per_contract=round(cand.max_loss_per_contract, 2),
            net_vega_per_contract=round(cand.net_vega_per_contract, 4),
            net_delta_per_contract=round(cand.net_delta_per_contract, 4),
        )
        projected.append(pos)
        book = risk.book_greeks(projected)

        alerts.entry_alert(cfg.discord_webhook, cand, verdict, decision, book, cfg.dry_run, label)

        if cfg.assume_filled and not cfg.dry_run:
            positions.append(pos)
            state.save_positions(cfg.positions_path, positions)
        elif cfg.dry_run:
            print(f"[dry-run] would open {pos.id}")
        else:
            print(f"[manual] confirm fill by adding {pos.id} to {cfg.positions_path}")
        return 0

    lines = ["candidates found but none cleared the risk layer"]
    for cand in candidates[:3]:
        v = risk.size_position(cand, open_pos, cfg)
        lines.append(f"· {cand.expiry} {cand.short.strike:g}/{cand.long.strike:g}: "
                     + "; ".join(v.reasons))
    alerts.status_alert(cfg.discord_webhook, "NO ENTRY", lines, True, cfg.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
