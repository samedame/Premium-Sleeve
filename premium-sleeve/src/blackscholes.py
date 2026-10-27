"""Black-Scholes greeks.

yfinance chains carry impliedVolatility but no greeks, so delta and vega
are computed here. Delta drives strike selection; vega drives the book
budget. Both matter enough to be tested rather than trusted.
"""
from __future__ import annotations

import math

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _d1(spot: float, strike: float, t: float, r: float, vol: float) -> float:
    return (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))


def put_delta(spot: float, strike: float, t: float, r: float, vol: float) -> float:
    """Put delta, negative. Returns -1.0/0.0 at the degenerate boundaries."""
    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return -1.0 if spot < strike else 0.0
    return _norm_cdf(_d1(spot, strike, t, r, vol)) - 1.0


def put_price(spot: float, strike: float, t: float, r: float, vol: float) -> float:
    if t <= 0 or vol <= 0:
        return max(strike - spot, 0.0)
    d1 = _d1(spot, strike, t, r, vol)
    d2 = d1 - vol * math.sqrt(t)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def vega(spot: float, strike: float, t: float, r: float, vol: float) -> float:
    """Vega per 1 percentage point of vol, per share.

    Textbook vega is per 1.00 of vol (i.e. 100 points); dividing by 100
    gives the per-point number the vega budget is written in.
    """
    if t <= 0 or vol <= 0:
        return 0.0
    return spot * _norm_pdf(_d1(spot, strike, t, r, vol)) * math.sqrt(t) / 100.0


def years_to_expiry(days: int) -> float:
    return max(days, 0) / 365.0
