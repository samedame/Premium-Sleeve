import datetime as dt
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dataclasses import replace
from src.blackscholes import put_delta, put_price, vega, years_to_expiry
from src.config import Config
from src.gate import evaluate
from src.risk import book_greeks, exit_check, kill_switch, size_position
from src.selector import Candidate, Leg
from src.state import Equity, Position, drawdown, record_close

FAILS = []

def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILS.append(name)

CFG = Config(account_capital=25000.0)
TODAY = dt.date(2026, 10, 27)

def leg(strike, delta, vg, bid, ask, oi=5000):
    return Leg(strike=strike, delta=delta, vega_per_share=vg, bid=bid, ask=ask, iv=0.18, open_interest=oi)

def candidate(expiry_days=38, short_k=745, long_k=710, credit_bid=(3.00, 3.10),
              long_q=(0.90, 0.96), short_vega=0.66, long_vega=0.30):
    return Candidate(
        expiry=TODAY + dt.timedelta(days=expiry_days), dte=expiry_days,
        short=leg(short_k, -0.30, short_vega, *credit_bid),
        long=leg(long_k, -0.10, long_vega, *long_q),
        spot=769.0,
    )

print("\n-- black-scholes --")
t = years_to_expiry(38)
check("ATM put delta near -0.5", -0.52 < put_delta(600, 600, t, 0.04, 0.18) < -0.42)
check("deep OTM put delta near 0", abs(put_delta(600, 400, t, 0.04, 0.18)) < 0.01)
check("delta monotone in strike",
      put_delta(600, 550, t, .04, .18) > put_delta(600, 590, t, .04, .18))
check("put price non-negative", put_price(600, 500, t, .04, .18) >= 0)
check("vega positive and small", 0 < vega(600, 600, t, .04, .18) < 2)
check("expired put is intrinsic", put_price(600, 650, 0, .04, .18) == 50)

print("\n-- gate --")
check("contango opens gate", evaluate(0.895, CFG).allow_entries)
check("backwardation closes gate", not evaluate(1.02, CFG).allow_entries)
check("exactly at threshold closes gate", not evaluate(1.00, CFG).allow_entries)

print("\n-- selector arithmetic --")
c = candidate()
check("credit = short mid - long mid", abs(c.credit - (3.05 - 0.93)) < 1e-9, f"got {c.credit}")
check("width correct", c.width == 35)
check("max loss = (width - credit) * 100",
      abs(c.max_loss_per_contract - (35 - 2.12) * 100) < 1e-6, f"got {c.max_loss_per_contract}")
check("spread is short vega", c.net_vega_per_contract < 0, f"got {c.net_vega_per_contract}")
check("bull put spread is long delta", c.net_delta_per_contract > 0, f"got {c.net_delta_per_contract}")

print("\n-- sizing --")
v = size_position(c, [], CFG)
# 5% of 25000 = 1250 cap; max loss 3288/ct -> 0 contracts. Should refuse.
check("refuses when one contract exceeds per-position cap", not v.allowed, v.reasons)

narrow = candidate(short_k=745, long_k=740, long_q=(2.10, 2.16), long_vega=0.62)
v2 = size_position(narrow, [], CFG)
check("allows a position that fits the cap", v2.allowed, v2.reasons)
if v2.allowed:
    risk_total = narrow.max_loss_per_contract * v2.contracts
    check("sized risk within 5% of capital", risk_total <= 25000 * 0.05 + 1e-6,
          f"risk {risk_total}")
    vega_total = abs(narrow.net_vega_per_contract * v2.contracts)
    check("sized vega within budget", vega_total <= 25000 * 0.0015 + 1e-6,
          f"vega {vega_total}")

unsized = size_position(narrow, [], Config(account_capital=None))
check("unsized mode allows but issues no count",
      unsized.allowed and unsized.contracts is None and unsized.unsized)

print("\n-- concurrency and stagger --")
def pos(expiry_days, n=1, ml=400.0, vg=-3.0):
    return Position(id=f"p{expiry_days}", opened=TODAY.isoformat(),
                    expiry=(TODAY + dt.timedelta(days=expiry_days)).isoformat(),
                    short_strike=745, long_strike=740, contracts=n,
                    credit=1.0, max_loss_per_contract=ml,
                    net_vega_per_contract=vg, net_delta_per_contract=2.0)

three = [pos(31), pos(45), pos(60)]
check("blocks at max concurrent", not size_position(narrow, three, CFG).allowed)
close = [pos(36)]  # narrow expires in 38d -> 2d gap, under 7d minimum
check("blocks insufficient stagger", not size_position(narrow, close, CFG).allowed)
far = [pos(20)]   # 18d gap, fine
check("allows sufficient stagger", size_position(narrow, far, CFG).allowed)

print("\n-- book greeks --")
bg = book_greeks([pos(31, n=2, vg=-3.0, ml=400.0), pos(45, n=1, vg=-3.0, ml=400.0)])
check("book vega sums across positions", abs(bg.vega - (-9.0)) < 1e-9, f"got {bg.vega}")
check("book risk sums across positions", abs(bg.risk - 1200.0) < 1e-9, f"got {bg.risk}")

print("\n-- vega budget binds --")
# capital 25k -> vega budget 37.5/pt. Load the book near the cap.
heavy = [pos(20, n=7, vg=-5.0, ml=100.0)]  # 35.0 of 37.5 vega used
v3 = size_position(narrow, heavy, CFG)
if v3.allowed:
    used = abs(book_greeks(heavy).vega) + abs(narrow.net_vega_per_contract * v3.contracts)
    check("vega budget respected when book already loaded", used <= 37.5 + 1e-6, f"used {used}")
else:
    check("vega budget blocks when exhausted", "vega" in " ".join(v3.reasons).lower(), v3.reasons)

print("\n-- exits --")
p = pos(40); p.credit = 2.00
check("profit target at 50%", exit_check(p, 1.00, TODAY, CFG)[0])
check("no exit at 40% captured", not exit_check(p, 1.20, TODAY, CFG)[0])
check("stop loss at 2x credit", exit_check(p, 4.00, TODAY, CFG)[0])
check("no exit just under stop", not exit_check(p, 3.90, TODAY, CFG)[0])
p21 = pos(21); p21.credit = 2.00
check("time exit at 21 DTE", exit_check(p21, 1.50, TODAY, CFG)[0])
p22 = pos(22); p22.credit = 2.00
check("no time exit at 22 DTE", not exit_check(p22, 1.50, TODAY, CFG)[0])

print("\n-- kill switch --")
eq = Equity(peak=10000.0, current=10000.0)
check("no halt at peak", not kill_switch(eq, CFG, 0.9)[0])
eq = record_close(Equity(peak=10000.0, current=10000.0), -1400.0, TODAY, "loss")
check("no halt at 14% drawdown", not kill_switch(eq, CFG, 0.9)[0], f"dd={drawdown(eq):.3f}")
eq2 = record_close(Equity(peak=10000.0, current=10000.0), -1500.0, TODAY, "loss")
check("halts at 15% drawdown", kill_switch(eq2, CFG, 0.9)[0], f"dd={drawdown(eq2):.3f}")

halted = Equity(peak=10000.0, current=8500.0, halted=True, sessions_since_halt=5)
check("stays halted before 20 sessions", kill_switch(halted, CFG, 0.5)[0])
halted.sessions_since_halt = 20
check("stays halted in backwardation even after 20 sessions",
      kill_switch(halted, CFG, 1.05)[0])
check("restarts after 20 sessions in contango",
      not kill_switch(halted, CFG, 0.90)[0])

from src.schedule_guard import decide, record

def utc(y, m, d, hh, mm):
    return dt.datetime(y, m, d, hh, mm, tzinfo=dt.timezone.utc)

# The two real runs that were wrongly dropped: the 20:40 UTC slot arrived at
# 01:05 UTC (after the close) and the 19:40 slot at 00:33 UTC. Those are
# outside the window and still skip -- but a run that lands in the afternoon
# must now go through, whatever its UTC hour.
check("entry runs at 18:40 UTC in EST (13:40 ET)",
      decide("entry", utc(2026, 11, 5, 18, 40), {})[0])
check("entry runs at 17:40 UTC in EDT (13:40 ET)",
      decide("entry", utc(2026, 10, 28, 17, 40), {})[0])
check("entry skips after the close (01:05 UTC = 20:05 ET)",
      not decide("entry", utc(2026, 11, 6, 1, 5), {})[0])
check("entry skips before the window (14:40 UTC EST = 09:40 ET)",
      not decide("entry", utc(2026, 11, 5, 14, 40), {})[0])
check("entry skips on a weekend",
      not decide("entry", utc(2026, 11, 7, 18, 40), {})[0])
done = record("entry", utc(2026, 11, 5, 18, 40), {})
check("entry runs once per ET date",
      not decide("entry", utc(2026, 11, 5, 19, 40), done)[0])
check("entry runs again the next day",
      decide("entry", utc(2026, 11, 6, 18, 40), done)[0])
check("dispatch bypasses the window",
      decide("entry", utc(2026, 11, 6, 1, 5), {}, "workflow_dispatch")[0])
m1 = record("monitor", utc(2026, 11, 5, 15, 10), {})
check("monitor waits 3h between runs",
      not decide("monitor", utc(2026, 11, 5, 17, 10), m1)[0])
check("monitor allows a second run after 3h",
      decide("monitor", utc(2026, 11, 5, 19, 10), m1)[0])
m2 = record("monitor", utc(2026, 11, 5, 19, 10), m1)
check("monitor stops at two runs a day",
      not decide("monitor", utc(2026, 11, 5, 20, 10), m2)[0])
old = record("entry", utc(2026, 10, 1, 18, 40), {})
check("old run records are pruned",
      "entry" not in record("monitor", utc(2026, 11, 5, 15, 10), old))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
