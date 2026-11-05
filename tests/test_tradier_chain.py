"""Offline tests for the Tradier-first chain adapter. Run: python tests/test_tradier_chain.py"""
import datetime as dt
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from src import data, tradier
from src import tradier_chain as tc

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILS.append(name)


def opt(strike, kind="put", bid=1.0, ask=1.2, mid_iv=0.2, oi=100, smv=None):
    g = {"mid_iv": mid_iv}
    if smv is not None:
        g["smv_vol"] = smv
    return {"symbol": f"SPY261218{kind[0].upper()}{int(strike*1000):08d}", "strike": strike, "option_type": kind,
            "bid": bid, "ask": ask, "last": 1.1, "volume": 5, "open_interest": oi, "greeks": g}


class FakeYF:
    """Stands in for the yfinance module: records which symbols it was asked for."""
    calls = []

    class Ticker:
        def __init__(self, s):
            FakeYF.calls.append(s)
            self.symbol = s
            self.options = ("2026-12-18",)

        def option_chain(self, e):
            puts = pd.DataFrame({"strike": [700.0], "bid": [1.0], "ask": [1.1], "openInterest": [7],
                                 "impliedVolatility": [0.19]})
            return SimpleNamespace(puts=puts, calls=puts)

        def history(self, **kw):
            return pd.DataFrame({"Close": [650.0]})


def patch(**fns):
    saved = {k: getattr(tradier, k) for k in fns}
    for k, v in fns.items():
        setattr(tradier, k, v)
    return lambda: [setattr(tradier, k, v) for k, v in saved.items()]


print("\n-- chain_frames --")
calls, puts = tc.chain_frames([opt(700), opt(710, "call"), opt(690, mid_iv=0, smv=0.25), {"strike": 1}])
check("puts and calls split", len(puts) == 2 and len(calls) == 1)
check("sorted by strike", list(puts["strike"]) == [690.0, 700.0])
check("IV falls back to smv_vol", abs(puts.iloc[0]["impliedVolatility"] - 0.25) < 1e-9)
check("openInterest mapped", puts.iloc[1]["openInterest"] == 100)
check("spot prefers last, then close", tc.spot_from_quote({"last": 5, "close": 4}) == 5 and
      tc.spot_from_quote({"last": None, "close": 4}) == 4 and tc.spot_from_quote({}) is None)

print("\n-- make_ticker --")
restore = patch(configured=lambda env=None: False)
check("no key -> plain yfinance ticker", isinstance(tc.make_ticker("SPY", FakeYF), FakeYF.Ticker))
restore()
restore = patch(configured=lambda env=None: True)
check("key -> fallback ticker", isinstance(tc.make_ticker("SPY", FakeYF), tc.FallbackTicker))
restore()

print("\n-- Tradier first, yfinance fallback --")
logs = []
FakeYF.calls.clear()
restore = patch(configured=lambda env=None: True, expirations=lambda s, **k: ["2026-12-18"],
                chain=lambda s, e, greeks=True, **k: [opt(700, mid_iv=0.21), opt(690, mid_iv=0.23)],
                quotes=lambda syms, **k: {syms[0]: {"last": 701.5}})
t = tc.FallbackTicker("SPY", FakeYF, log=logs.append)
check("expirations from Tradier", t.options == ("2026-12-18",))
ch = t.option_chain("2026-12-18")
check("chain from Tradier with IV", list(ch.puts["impliedVolatility"]) == [0.23, 0.21])
check("spot from Tradier", float(t.history(period="5d", interval="1d")["Close"].iloc[-1]) == 701.5)
check("yfinance never touched", FakeYF.calls == [] and logs == [])
restore()

def boom(*a, **k):
    raise tradier.TradierError("HTTP 500")

restore = patch(configured=lambda env=None: True, expirations=boom, chain=boom, quotes=boom)
logs.clear()
t = tc.FallbackTicker("SPY", FakeYF, log=logs.append)
check("expirations fall back", t.options == ("2026-12-18",))
check("chain falls back", float(t.option_chain("2026-12-18").puts.iloc[0]["strike"]) == 700.0)
check("spot falls back", float(t.history(period="5d", interval="1d")["Close"].iloc[-1]) == 650.0)
check("each fallback is logged", len(logs) == 3 and all("using yfinance" in l for l in logs))
restore()

restore = patch(configured=lambda env=None: True, chain=lambda *a, **k: [opt(700, mid_iv=0)])
check("chain with no IV falls back", tc.FallbackTicker("SPY", FakeYF, log=logs.append)
      .option_chain("2026-12-18").puts.iloc[0]["impliedVolatility"] == 0.19)
restore()
restore = patch(configured=lambda env=None: True, expirations=lambda *a, **k: [])
check("empty expirations fall back", tc.FallbackTicker("SPY", FakeYF, log=logs.append).options == ("2026-12-18",))
restore()

print("\n-- data.py end to end --")
data.yf = FakeYF
data._retry = lambda fn, what, *a, **k: fn()
today = dt.datetime.now(dt.timezone.utc).date()
exp = (today + dt.timedelta(days=38)).isoformat()
restore = patch(configured=lambda env=None: True, expirations=lambda s, **k: [exp],
                chain=lambda s, e, greeks=True, **k: [opt(700, mid_iv=0.2, oi=42), opt(650, mid_iv=0)],
                quotes=lambda syms, **k: {syms[0]: {"last": 705.0}})
chains = data.put_chains("SPY", 30, 45)
check("put_chains returns Tradier quotes", len(chains) == 1 and chains[0].puts[0].open_interest == 42)
check("zero-IV strike still filtered", len(chains[0].puts) == 1)
q = data.quote_for("SPY", today + dt.timedelta(days=38), 700.0)
check("quote_for finds the strike", q is not None and q.bid == 1.0)
check("quote_for unlisted strike is None", data.quote_for("SPY", today + dt.timedelta(days=38), 123.0) is None)
check("spot from Tradier", data.spot("SPY") == 705.0)
restore()

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:", FAILS)
    sys.exit(1)
print("all passed")
