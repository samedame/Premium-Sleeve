"""ADVISORY-ONLY assignment-risk line for open short put legs.

SPY options are American-style and physically settled. A short put that is in the money with almost no extrinsic value can be
exercised at any time; if the long put is out of the money the assignment leaves an unhedged share position, and a weekend gap
then lands on shares, not on a capped spread. This module only describes that risk in one extra alert.

It never changes entry, exit, sizing, the gate, the risk layer or the saved state files. ASSIGNMENT_GUARD=0 turns it off; with it off
monitor.py behaves exactly like the unguarded version (tests/test_assignment_guard.py differential test). Any failure in the guard is
swallowed and printed: it must never break the monitor.

Extrinsic value uses the live quote mid. The ex-dividend date is an ESTIMATE (third Friday of Mar/Jun/Sep/Dec).
"""
from __future__ import annotations

import datetime as dt
import os

ENV = "ASSIGNMENT_GUARD"
EXTRINSIC_MAX = 0.05
EXDIV_WINDOW_DAYS = 3
CLOSE_BY = "15:55 ET"


def enabled() -> bool:
    return os.environ.get(ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _third_friday(y: int, m: int) -> dt.date:
    d = dt.date(y, m, 1)
    d += dt.timedelta(days=(4 - d.weekday()) % 7)
    return d + dt.timedelta(days=14)


def next_exdiv_estimate(today: dt.date) -> dt.date:
    for y in (today.year, today.year + 1):
        for m in (3, 6, 9, 12):
            d = _third_friday(y, m)
            if d >= today:
                return d
    raise AssertionError("unreachable")


def assess(pos, spot: float, short_mid: float, today: dt.date) -> dict | None:
    """None unless the short put is ITM AND (extrinsic < $0.05, or <= 1 day to expiry, or an estimated ex-dividend date is near)."""
    if not spot or not short_mid or short_mid <= 0 or not spot < pos.short_strike:
        return None
    intrinsic = pos.short_strike - spot
    extrinsic = max(short_mid - intrinsic, 0.0)
    dte = pos.dte(today)
    exdiv_days = (next_exdiv_estimate(today) - today).days
    reasons = []
    if extrinsic < EXTRINSIC_MAX:
        reasons.append("almost no extrinsic value left")
    if dte <= 1:
        reasons.append("expires today or tomorrow")
    if exdiv_days <= EXDIV_WINDOW_DAYS:
        reasons.append("ex-dividend is near")
    if not reasons:
        return None
    return dict(id=pos.id, short=pos.short_strike, long=pos.long_strike, contracts=pos.contracts, spot=spot, itm_by=intrinsic,
                extrinsic=extrinsic, exdiv_days=exdiv_days, dte=dte, reasons=reasons)


def line(a: dict) -> str:
    n = a["contracts"]
    shares = f"{int(n) * 100:,} shares" if n else "100 shares per contract"
    return (f"ASSIGNMENT RISK: short leg ITM, extrinsic ${a['extrinsic']:.2f}, ex-div in {a['exdiv_days']} days (est.), "
            f"close/roll before {CLOSE_BY} today. [{a['id']}] SPY {a['spot']:,.2f} is {a['itm_by']:.2f} below the {a['short']:g}P short "
            f"({a['reasons'][0]}; {a['dte']} DTE). If assigned while the {a['long']:g}P is out of the money you hold up to {shares} with no cap on a gap.")


def check(cfg, positions, today: dt.date, quote_for, spot_fn) -> list[str]:
    """Advisory lines for the still-open positions. Never raises."""
    out: list[str] = []
    try:
        spot = None
        for pos in positions:
            if pos.status != "open":
                continue
            if spot is None:
                spot = spot_fn(cfg.underlying)
            q = quote_for(cfg.underlying, pos.expiry_date, pos.short_strike)
            if q is None:
                continue
            a = assess(pos, spot, q.mid, today)
            if a:
                out.append(line(a))
    except Exception as exc:   # noqa: BLE001 - advisory only
        print(f"[assignment_guard] skipped: {exc}")
    return out
