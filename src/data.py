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

from . import tradier

# A leg quote older than this is not trustworthy for an alert: the entry/exit message is replaced by a NOTICE.
MAX_QUOTE_AGE_MIN = 30.0
SOURCE_TRADIER = "tradier"
SOURCE_YFINANCE = "yfinance"
DELAYED_LABEL = "DELAYED/UNVERIFIED"

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
    age_min: float | None = None   # minutes since the quote/trade stamp; None = the source gave no timestamp
    source: str = ""               # SOURCE_TRADIER | SOURCE_YFINANCE ("" = unlabelled, e.g. tests)

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
    source: str = ""


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _yf_spot(ticker: str) -> float:
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


def _yf_put_chains(ticker: str, dte_min: int, dte_max: int) -> list[Chain]:
    """Every put chain whose expiry falls inside the DTE window (yfinance: delayed, unverified)."""
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
        quotes = [q for q in yf_quotes(frame) if q.iv > 0 and q.strike > 0]
        if quotes:
            chains.append(Chain(expiry=expiry, dte=dte, puts=sorted(quotes, key=lambda q: q.strike),
                                source=SOURCE_YFINANCE))
    if not chains:
        raise DataError(f"no {ticker} expiries between {dte_min} and {dte_max} DTE")
    return chains


def _yf_quote_for(ticker: str, expiry: dt.date, strike: float) -> OptionQuote | None:
    """Re-quote one put leg from yfinance.

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
            return yf_quotes(frame[frame["strike"] == row.strike])[0]
    return None


# --------------------------------------------------------------------------- parsing (pure; unit-tested on fixtures)

def _num(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f      # NaN -> default


def yf_quotes(frame, now: float | None = None) -> list[OptionQuote]:
    """OptionQuotes from a yfinance puts frame. Same field handling as before, plus the age of `lastTradeDate`.

    NOTE: lastTradeDate is the last TRADE of that contract, not the last quote, so it is an upper bound on staleness.
    """
    now = time.time() if now is None else now
    have_ts = "lastTradeDate" in getattr(frame, "columns", [])
    out = []
    for row in frame.itertuples():
        age = None
        if have_ts:
            try:
                age = max(0.0, now - row.lastTradeDate.timestamp()) / 60.0
            except Exception:  # noqa: BLE001 - NaT / missing / naive timestamps -> unknown age
                age = None
        out.append(OptionQuote(
            strike=float(row.strike),
            bid=float(row.bid or 0.0),
            ask=float(row.ask or 0.0),
            iv=float(row.impliedVolatility or 0.0),
            open_interest=int(row.openInterest or 0),
            age_min=age,
            source=SOURCE_YFINANCE,
        ))
    return out


def tradier_put_quotes(options: list[dict], now: float | None = None) -> list[OptionQuote]:
    """OptionQuotes from Tradier /markets/options/chains rows (puts only, greeks=true for the IV)."""
    now = time.time() if now is None else now
    out = []
    for o in options:
        if o.get("option_type") != "put":
            continue
        g = o.get("greeks") or {}
        iv = _num(g.get("mid_iv")) or _num(g.get("smv_vol"))
        age_s = tradier.quote_age_s(o, now=now)
        out.append(OptionQuote(
            strike=_num(o.get("strike")),
            bid=_num(o.get("bid")),
            ask=_num(o.get("ask")),
            iv=iv,
            open_interest=int(_num(o.get("open_interest"))),
            age_min=None if age_s is None else age_s / 60.0,
            source=SOURCE_TRADIER,
        ))
    return out


def quote_problem(q: OptionQuote, max_age_min: float = MAX_QUOTE_AGE_MIN) -> str | None:
    """Why this leg quote cannot back an alert, or None if it can. Unknown age is not, by itself, a problem.

    The age limit applies to Tradier quotes only: their bid/ask/trade stamps are true quote times. yfinance's
    lastTradeDate is merely the contract's last trade, often hours old for a perfectly live quote, so a yfinance leg is
    labelled DELAYED/UNVERIFIED instead of being rejected for it.
    """
    if q.bid <= 0:
        return f"{q.strike:g}P has no bid (bid <= 0)"
    if q.ask <= 0:
        return f"{q.strike:g}P has no ask (ask <= 0)"
    if q.source == SOURCE_TRADIER and q.age_min is not None and q.age_min > max_age_min:
        return f"{q.strike:g}P quote is {q.age_min:.0f} min old (limit {max_age_min:.0f})"
    return None


def data_label(quotes: list[OptionQuote]) -> str:
    """One line for the Discord alert saying where the prices came from. yfinance is ALWAYS labelled."""
    sources = {q.source for q in quotes if q.source}
    ages = [q.age_min for q in quotes if q.age_min is not None]
    if SOURCE_YFINANCE in sources or not sources:
        age_txt = f"; last trade on the oldest leg {max(ages):.0f} min ago" if ages else ""
        return f"yfinance {DELAYED_LABEL}: Tradier unavailable or not configured{age_txt}"
    age_txt = f", oldest leg quote {max(ages):.1f} min old" if ages else ""
    return f"Tradier{age_txt}"


# --------------------------------------------------------------------------- public API: Tradier first, yfinance fallback

def _log(msg: str) -> None:
    print(f"[data] {msg}")


def spot(ticker: str) -> float:
    if tradier.configured():
        try:
            qv = tradier.quotes([ticker]).get(ticker.upper())
            for k in ("last", "close"):
                v = _num((qv or {}).get(k))
                if v > 0:
                    return v
            err = "no price in quote"
        except Exception as exc:  # noqa: BLE001 - any Tradier failure falls back
            err = f"{type(exc).__name__}: {exc}"
        _log(f"{ticker} spot: Tradier unavailable ({err}); using yfinance")
    return _yf_spot(ticker)


def put_chains(ticker: str, dte_min: int, dte_max: int) -> list[Chain]:
    """Every put chain whose expiry falls inside the DTE window. Tradier first; a labelled yfinance fallback."""
    if tradier.configured():
        try:
            today = _today()
            chains: list[Chain] = []
            for raw in tradier.expirations(ticker):
                expiry = dt.date.fromisoformat(raw)
                dte = (expiry - today).days
                if not (dte_min <= dte <= dte_max):
                    continue
                quotes = [q for q in tradier_put_quotes(tradier.chain(ticker, raw, greeks=True))
                          if q.iv > 0 and q.strike > 0]
                if quotes:
                    chains.append(Chain(expiry=expiry, dte=dte, puts=sorted(quotes, key=lambda q: q.strike),
                                        source=SOURCE_TRADIER))
            if chains:
                return chains
            err = "no usable chains in the DTE window"
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        _log(f"{ticker} chains: Tradier unavailable ({err}); using yfinance, {DELAYED_LABEL}")
    return _yf_put_chains(ticker, dte_min, dte_max)


def quote_for(ticker: str, expiry: dt.date, strike: float) -> OptionQuote | None:
    """Re-quote one put leg (Tradier first). None only when the strike is genuinely not listed; a data failure raises
    DataError so "not listed" is never confused with "could not see the price"."""
    if tradier.configured():
        try:
            raw = expiry.isoformat()
            if raw in tradier.expirations(ticker):
                for q in tradier_put_quotes(tradier.chain(ticker, raw, greeks=True)):
                    if abs(q.strike - strike) < 1e-6:
                        return q
                return None
            return None
        except Exception as exc:  # noqa: BLE001
            _log(f"{ticker} {expiry} {strike:g}P: Tradier unavailable ({type(exc).__name__}: {exc}); "
                 f"using yfinance, {DELAYED_LABEL}")
    return _yf_quote_for(ticker, expiry, strike)
