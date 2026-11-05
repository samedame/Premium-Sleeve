"""yfinance-shaped views over Tradier, so data.py's option-chain and spot code stays as it was.

`make_ticker(symbol)` returns an object that quacks like yfinance.Ticker for the three things the sleeve uses:
.options (expiry strings), .option_chain(expiry).puts / .calls (columns contractSymbol, strike, bid, ask, lastPrice,
volume, openInterest, impliedVolatility) and .history(...)["Close"] (the spot price). With a Tradier key every call asks
Tradier first and falls back to the real yfinance object if Tradier raises or answers empty, so a bad key, a rate limit
or an outage can only ever cost freshness. Each fallback is printed to the run log.

impliedVolatility comes from Tradier's greeks (mid_iv, else smv_vol). The sleeve drops any strike whose IV is zero, so the
chain is requested WITH greeks: without them every strike would vanish. Tradier's IV is a different estimate from Yahoo's,
so expect small differences in the IV the selector sees. A sandbox key works but its data is about 15 minutes delayed.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pandas as pd

from . import tradier

COLUMNS = ["contractSymbol", "strike", "bid", "ask", "lastPrice", "volume", "openInterest", "impliedVolatility"]


def _num(v, default=0.0):
    try:
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def _iv(o) -> float:
    g = o.get("greeks") or {}
    for k in ("mid_iv", "smv_vol"):
        v = _num(g.get(k))
        if v > 0:
            return v
    return 0.0


def chain_frames(options):
    """(calls, puts) DataFrames from Tradier option dicts."""
    rows = [{"contractSymbol": o.get("symbol"), "strike": _num(o.get("strike")), "bid": _num(o.get("bid")),
             "ask": _num(o.get("ask")), "lastPrice": _num(o.get("last")), "volume": _num(o.get("volume")),
             "openInterest": _num(o.get("open_interest")), "impliedVolatility": _iv(o), "_type": o.get("option_type")}
            for o in options if o.get("symbol") and o.get("strike") is not None]
    df = pd.DataFrame(rows, columns=COLUMNS + ["_type"])
    out = []
    for kind in ("call", "put"):
        out.append(df[df["_type"] == kind].drop(columns="_type").sort_values("strike").reset_index(drop=True))
    return out[0], out[1]


def spot_from_quote(q):
    """The last trade, else the official close. None if the quote has neither."""
    for k in ("last", "close"):
        v = _num((q or {}).get(k), 0.0)
        if v > 0:
            return v
    return None


class TradierTicker:
    """The Tradier half. Raises on any failure; FallbackTicker catches it."""

    def __init__(self, symbol):
        self.symbol = symbol.upper()

    @property
    def options(self):
        return tuple(tradier.expirations(self.symbol))

    def option_chain(self, expiry):
        calls, puts = chain_frames(tradier.chain(self.symbol, expiry, greeks=True))
        return SimpleNamespace(calls=calls, puts=puts)

    def history(self, period="5d", interval="1d", **_):
        spot = spot_from_quote(tradier.quotes([self.symbol]).get(self.symbol))
        if spot is None:
            raise tradier.TradierError(f"no price for {self.symbol}")
        return pd.DataFrame({"Close": [spot]}, index=[pd.Timestamp(dt.date.today())])


class FallbackTicker:
    def __init__(self, symbol, yf, log=print):
        self.symbol, self._yf, self._log = symbol, yf, log
        self._tr = TradierTicker(symbol)

    def _fallback(self, what, err):
        self._log(f"  [data] {self.symbol} {what}: Tradier unavailable ({err}); using yfinance")
        return self._yf.Ticker(self.symbol)

    @property
    def options(self):
        try:
            exps = self._tr.options
            if exps:
                return exps
            err = "no expirations"
        except Exception as e:      # noqa: BLE001 -- anything Tradier does wrong falls back
            err = f"{type(e).__name__}: {e}"
        return self._fallback("expirations", err).options

    def option_chain(self, expiry):
        try:
            chain = self._tr.option_chain(expiry)
            if not chain.puts.empty and (chain.puts["impliedVolatility"] > 0).any():
                return chain
            err = "empty chain or no implied volatility"
        except Exception as e:      # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
        return self._fallback(f"chain {expiry}", err).option_chain(expiry)

    def history(self, period="5d", interval="1d", **kw):
        try:
            return self._tr.history(period, interval)
        except Exception as e:      # noqa: BLE001
            return self._fallback("spot", f"{type(e).__name__}: {e}").history(period=period, interval=interval, **kw)


def make_ticker(symbol, yf):
    """Tradier-first ticker when a key is configured, else the plain yfinance one (behaviour unchanged)."""
    return FallbackTicker(symbol, yf) if tradier.configured() else yf.Ticker(symbol)
