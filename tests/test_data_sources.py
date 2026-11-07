"""Tradier-first data layer: parsing, labelling, quote gate, and IDENTICAL strike selection whichever source fed it.

Same style as test_sleeve.py (plain script, no network):  python3 tests/test_data_sources.py
"""
import datetime as dt
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import alerts, data, entry, tradier
from src.blackscholes import put_price, years_to_expiry
from src.config import Config
from src.selector import build_candidates

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILS.append(name)


NOW = 1_800_000_000.0           # fixed "now" (epoch seconds)
SPOT = 769.0
DTE = 38
EXPIRY = dt.date.today() + dt.timedelta(days=DTE)
CFG = Config(account_capital=25000.0)


def raw_rows():
    """(strike, bid, ask, iv, oi) for a SPY-like put ladder, priced off Black-Scholes at 18% IV."""
    t, rows = years_to_expiry(DTE), []
    for k in range(640, 770, 5):
        mid = put_price(SPOT, k, t, CFG.risk_free_rate, 0.18)
        half = max(0.02, mid * 0.015)
        rows.append((float(k), round(max(0.01, mid - half), 2), round(mid + half, 2), 0.18, 5000))
    return rows


def tradier_options(age_s=5.0, bid_override=None):
    out = []
    for i, (k, b, a, iv, oi) in enumerate(raw_rows()):
        stamp = int((NOW - age_s) * 1000)
        out.append({"symbol": f"SPY{k}", "strike": k, "option_type": "put", "bid": b, "ask": a, "open_interest": oi,
                    "bid_date": stamp, "ask_date": stamp, "greeks": {"mid_iv": iv}})
    out.append({"symbol": "SPYC", "strike": 700.0, "option_type": "call", "bid": 1, "ask": 2, "greeks": {"mid_iv": .2}})
    if bid_override is not None:
        out[3]["bid"] = bid_override
    return out


class Row:  # a yfinance puts-frame row
    def __init__(self, k, b, a, iv, oi, ts):
        self.strike, self.bid, self.ask, self.impliedVolatility, self.openInterest = k, b, a, iv, oi
        self.lastTradeDate = ts


class Frame:
    columns = ["strike", "bid", "ask", "impliedVolatility", "openInterest", "lastTradeDate"]

    def __init__(self, rows):
        self.rows = rows

    def itertuples(self):
        return iter(self.rows)


def yf_frame(age_s=7200.0):
    ts = types.SimpleNamespace(timestamp=lambda: NOW - age_s)
    return Frame([Row(k, b, a, iv, oi, ts) for k, b, a, iv, oi in raw_rows()])


def chain_from(quotes, source):
    return data.Chain(expiry=EXPIRY, dte=DTE, puts=sorted(quotes, key=lambda q: q.strike), source=source)


print("\n-- parsing --")
tq = data.tradier_put_quotes(tradier_options(age_s=90.0), now=NOW)
yq = data.yf_quotes(yf_frame(), now=NOW)
check("Tradier parser keeps puts only", len(tq) == len(raw_rows()) and all(q.source == "tradier" for q in tq))
check("Tradier IV comes from greeks.mid_iv", all(abs(q.iv - 0.18) < 1e-9 for q in tq))
check("Tradier quote age is computed from bid/ask stamps (90 s -> 1.5 min)", all(abs(q.age_min - 1.5) < 1e-6 for q in tq))
check("yfinance age is computed from lastTradeDate (2 h -> 120 min)", all(abs(q.age_min - 120.0) < 1e-6 for q in yq))
check("no timestamp -> age None (not a fake 0.0)",
      data.tradier_put_quotes([{"option_type": "put", "strike": 1, "bid": 1, "ask": 2}], now=NOW)[0].age_min is None)

print("\n-- identical strike selection on the same fixture --")
spot_px = SPOT
c_tr, _ = build_candidates([chain_from(tq, "tradier")], spot_px, CFG)
c_yf, _ = build_candidates([chain_from(yq, "yfinance")], spot_px, CFG)
check("fixture yields a candidate", len(c_tr) == 1 and len(c_yf) == 1, f"{len(c_tr)} / {len(c_yf)}")
if c_tr and c_yf:
    a, b = c_tr[0], c_yf[0]
    check("same short strike", a.short.strike == b.short.strike, f"{a.short.strike} vs {b.short.strike}")
    check("same long strike", a.long.strike == b.long.strike)
    check("same credit and deltas", a.credit == b.credit and a.short.delta == b.short.delta)
# the selector is source-blind: metadata never reaches it
check("OptionQuote default fields keep old call sites working",
      data.OptionQuote(strike=1, bid=1, ask=2, iv=.2, open_interest=3).age_min is None)

print("\n-- quote gate --")
ok = data.OptionQuote(700, 1.0, 1.1, .18, 500, age_min=1.0, source="tradier")
check("good Tradier quote passes", data.quote_problem(ok) is None)
check("bid <= 0 rejected", "no bid" in (data.quote_problem(data.OptionQuote(700, 0, 1.1, .18, 500, 1.0, "tradier")) or ""))
check("ask <= 0 rejected", "no ask" in (data.quote_problem(data.OptionQuote(700, 1, 0, .18, 500, 1.0, "tradier")) or ""))
check("Tradier age over MAX_QUOTE_AGE_MIN rejected",
      "min old" in (data.quote_problem(data.OptionQuote(700, 1, 1.1, .18, 500, data.MAX_QUOTE_AGE_MIN + 1, "tradier")) or ""))
check("Tradier age exactly at the limit still passes",
      data.quote_problem(data.OptionQuote(700, 1, 1.1, .18, 500, data.MAX_QUOTE_AGE_MIN, "tradier")) is None)
check("yfinance last-trade age is labelled, not rejected",
      data.quote_problem(data.OptionQuote(700, 1, 1.1, .18, 500, 9999, "yfinance")) is None)
check("yfinance data is always labelled DELAYED/UNVERIFIED", "DELAYED/UNVERIFIED" in data.data_label(yq))
check("Tradier data is not labelled delayed", "DELAYED" not in data.data_label(tq))

print("\n-- entry: notice instead of alert --")


def run_entry(quotes, source):
    sent = {"notice": [], "entry": []}
    saved = (data.vix_ratio, data.spot, data.put_chains, alerts.status_alert, alerts.entry_alert)
    data.vix_ratio = lambda: (15.0, 17.0, 0.88)
    data.spot = lambda t: SPOT
    data.put_chains = lambda t, lo, hi: [chain_from(quotes, source)]
    alerts.status_alert = lambda hook, title, lines, ok_, dry: sent["notice"].append((title, lines)) or True
    alerts.entry_alert = lambda *a, **k: sent["entry"].append((a, k)) or True
    cwd = os.getcwd()
    os.environ["DRY_RUN"] = "1"
    try:
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            os.makedirs("state")
            rc = entry.main()
    finally:
        os.chdir(cwd)
        data.vix_ratio, data.spot, data.put_chains, alerts.status_alert, alerts.entry_alert = saved
        os.environ.pop("DRY_RUN", None)
    return rc, sent


rc, sent = run_entry(tq, "tradier")
check("fresh Tradier chain sends the entry alert", rc == 0 and len(sent["entry"]) == 1 and not sent["notice"], str(sent["notice"]))
check("entry alert carries the data label", sent["entry"] and "Tradier" in sent["entry"][0][1].get("data_label", sent["entry"][0][0][-1]))
rc, sent = run_entry(data.tradier_put_quotes(tradier_options(age_s=3600.0), now=NOW), "tradier")
check("stale Tradier legs -> NOTICE, no entry alert",
      rc == 0 and not sent["entry"] and sent["notice"] and "NOTICE" in sent["notice"][0][0], str(sent))
rc, sent = run_entry(yq, "yfinance")
check("yfinance fallback still alerts, labelled DELAYED/UNVERIFIED",
      len(sent["entry"]) == 1 and "DELAYED/UNVERIFIED" in str(sent["entry"][0]))

print("\n-- Tradier client is read-only and host-locked --")
try:
    tradier._get("/accounts/123/orders", env={"TRADIER_API_KEY": "x"})
    check("refuses non-market paths", False)
except tradier.TradierError:
    check("refuses non-market paths", True)
try:
    tradier.mode({"TRADIER_BASE_URL": "https://evil.example.com/v1"})
    check("refuses non-Tradier hosts", False)
except tradier.TradierError:
    check("refuses non-Tradier hosts", True)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
