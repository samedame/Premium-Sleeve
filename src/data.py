"""Market data via yfinance.

Snapshot only. This is a live-pricing source, not a backtesting source:
it has no point-in-time history and no historical bid/ask. Nothing here
should ever be used to claim a backtested result.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass

import yfinance as yf

_RETRIES = 4
_BACKOFF = 3.0


def _retry(fn, what: str):
    """Yahoo rate-limits (HTTP 429) unpredictably; back off and retry.

    Raises DataError rather than returning empty, so a throttled run
    fails loudly instead of being mistaken for 'no candidates'.
    """
    last = None
    for attempt in range(_RETRIES):
        try:
            out = fn()
            if out is not None and not (hasattr(out, "__len__") and len(out) == 0):
                return out
            last = f"empty response for {what}"
        except Exception as exc:  # noqa: BLE001 - yfinance raises broadly
            last = f"{type(exc).__name__}: {exc}"
        if attempt < _RETRIES - 1:
            time.sleep(_BACKOFF * (2 ** attempt))
    raise DataError(f"{what} unavailable after {_RETRIES} attempts ({last})")


class DataError(RuntimeError):
    """Raised when market data is missing or unusable."""


@dataclass
class OptionQuote:
    strike: float
    bid: float
    ask: float
    iv: float
    open_interest: int

    @property
    def mid(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 0.0
        return (self.bid + self.ask) / 2.0

    @property
    def width(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return float("inf")
        return self.ask - self.bid


@dataclass
class Chain:
    expiry: dt.date
    dte: int
    puts: list[OptionQuote]


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def spot(ticker: str) -> float:
    hist = _retry(
        lambda: yf.Ticker(ticker).history(period="5d", interval="1d"),
        f"price history for {ticker}",
    )
    return float(hist["Close"].iloc[-1])


def vix_ratio() -> tuple[float, float, float]:
    """Return (VIX, VIX3M, VIX/VIX3M).

    The ratio is the sleeve's only regime input. >= 1.00 is
    backwardation and blocks new entries.
    """
    out: dict[str, float] = {}
    for sym in ("^VIX", "^VIX3M"):
        hist = _retry(
            lambda s=sym: yf.Ticker(s).history(period="5d", interval="1d"),
            f"history for {sym}",
        )
        out[sym] = float(hist["Close"].iloc[-1])
    vix, vix3m = out["^VIX"], out["^VIX3M"]
    if vix3m <= 0:
        raise DataError("VIX3M non-positive")
    return vix, vix3m, vix / vix3m


def put_chains(ticker: str, dte_min: int, dte_max: int) -> list[Chain]:
    """Every put chain whose expiry falls inside the DTE window."""
    tk = yf.Ticker(ticker)
    expiries = _retry(lambda: tk.options, f"option expiries for {ticker}")

    today = _today()
    chains: list[Chain] = []
    for raw in expiries:
        expiry = dt.date.fromisoformat(raw)
        dte = (expiry - today).days
        if not (dte_min <= dte <= dte_max):
            continue
        frame = _retry(lambda r=raw: tk.option_chain(r).puts, f"{ticker} {raw} puts")
        quotes = [
            OptionQuote(
                strike=float(row.strike),
                bid=float(row.bid or 0.0),
                ask=float(row.ask or 0.0),
                iv=float(row.impliedVolatility or 0.0),
                open_interest=int(row.openInterest or 0),
            )
            for row in frame.itertuples()
        ]
        quotes = [q for q in quotes if q.iv > 0 and q.strike > 0]
        if quotes:
            chains.append(Chain(expiry=expiry, dte=dte, puts=sorted(quotes, key=lambda q: q.strike)))
    if not chains:
        raise DataError(f"no {ticker} expiries between {dte_min} and {dte_max} DTE")
    return chains


def quote_for(ticker: str, expiry: dt.date, strike: float) -> OptionQuote | None:
    """Re-quote one put leg.

    Returns None only when the strike genuinely is not listed. A data
    failure raises DataError instead: the caller must be able to tell
    "this strike does not exist" from "I could not see the price",
    because the second must never be mistaken for "no exit needed".
    """
    tk = yf.Ticker(ticker)
    raw = expiry.isoformat()
    expiries = _retry(lambda: tk.options, f"option expiries for {ticker}")
    if raw not in expiries:
        return None
    frame = _retry(lambda: tk.option_chain(raw).puts, f"{ticker} {raw} puts")
    for row in frame.itertuples():
        if abs(float(row.strike) - strike) < 1e-6:
            return OptionQuote(
                strike=float(row.strike),
                bid=float(row.bid or 0.0),
                ask=float(row.ask or 0.0),
                iv=float(row.impliedVolatility or 0.0),
                open_interest=int(row.openInterest or 0),
            )
    return None
