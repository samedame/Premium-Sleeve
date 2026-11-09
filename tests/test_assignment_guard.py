"""Assignment guard: unit tests + a differential test of monitor.py against a pinned copy of the unguarded monitor
(tests/_baseline/monitor_baseline.py = src/monitor.py from main before the guard). Run: python -m pytest tests/test_assignment_guard.py"""
import datetime as dt
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import alerts, assignment_guard as AG, data, monitor  # noqa: E402
from src.data import OptionQuote  # noqa: E402
from src.state import Position  # noqa: E402
from tests._baseline import monitor_baseline  # noqa: E402

TODAY = dt.datetime.now(dt.timezone.utc).date()


def P(pid, short, long, days, credit, status="open", contracts=2):
    return Position(id=pid, opened=(TODAY - dt.timedelta(days=5)).isoformat(), expiry=(TODAY + dt.timedelta(days=days)).isoformat(),
                    short_strike=short, long_strike=long, contracts=contracts, credit=credit, max_loss_per_contract=3000.0,
                    net_vega_per_contract=-10.0, net_delta_per_contract=20.0, status=status)


def test_exdiv_estimate_and_third_friday():
    assert AG._third_friday(2026, 12) == dt.date(2026, 12, 18)
    assert AG.next_exdiv_estimate(dt.date(2026, 11, 6)) == dt.date(2026, 12, 18)
    assert AG.next_exdiv_estimate(dt.date(2026, 12, 19)) == dt.date(2027, 3, 19)


def test_assess_rules():
    p = P("x", 700, 665, 40, 1.8)
    assert AG.assess(p, 710.0, 0.5, TODAY) is None                                 # OTM
    assert AG.assess(p, 699.0, 3.0, TODAY) is None                                 # ITM by 1 with $2 extrinsic, far expiry, ex-div not near
    a = AG.assess(p, 699.0, 1.02, TODAY)                                           # extrinsic 0.02
    assert a and a["extrinsic"] == pytest.approx(0.02) and "almost no extrinsic value left" in a["reasons"]
    assert AG.assess(P("y", 700, 665, 1, 1.8), 699.0, 3.0, TODAY)["reasons"] == ["expires today or tomorrow"] or "expires today or tomorrow" in AG.assess(P("y", 700, 665, 1, 1.8), 699.0, 3.0, TODAY)["reasons"]
    assert AG.assess(p, None, 1.0, TODAY) is None and AG.assess(p, 699.0, 0.0, TODAY) is None


def test_line_has_required_text():
    a = AG.assess(P("x", 700, 665, 40, 1.8), 699.0, 1.02, TODAY)
    t = AG.line(a)
    assert t.startswith("ASSIGNMENT RISK: short leg ITM, extrinsic $0.02, ex-div in ") and "(est.), close/roll before 15:55 ET today" in t


def test_env_switch(monkeypatch):
    monkeypatch.delenv("ASSIGNMENT_GUARD", raising=False); assert AG.enabled()
    monkeypatch.setenv("ASSIGNMENT_GUARD", "0"); assert not AG.enabled()


def test_check_never_raises_and_skips_closed():
    def boom(*a, **k): raise RuntimeError("network down")
    cfg = type("C", (), {"underlying": "SPY"})()
    assert AG.check(cfg, [P("x", 700, 665, 40, 1.8)], TODAY, boom, lambda t: 699.0) == []
    q = lambda t, e, k: OptionQuote(strike=k, bid=1.0, ask=1.04, iv=0.2, open_interest=100)
    assert AG.check(cfg, [P("x", 700, 665, 40, 1.8, status="closed")], TODAY, q, lambda t: 699.0) == []
    assert len(AG.check(cfg, [P("x", 700, 665, 40, 1.8)], TODAY, q, lambda t: 699.0)) == 1


# ------------------------- differential test -------------------------
def quote_book():
    def d(days): return (TODAY + dt.timedelta(days=days)).isoformat()
    return {(d(40), 700.0): (1.00, 1.04), (d(40), 665.0): (0.04, 0.06),      # A: ITM by 1, extrinsic 0.02, stays open
            (d(40), 690.0): (0.80, 0.84), (d(40), 650.0): (0.10, 0.12),      # B: OTM, stays open
            (d(41), 705.0): (7.90, 8.10), (d(41), 670.0): (0.30, 0.40)}      # C: ITM, hits the stop, closes this run


def scenario(mon, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("state", exist_ok=True)
    pos = [P("aaa", 700, 665, 40, 1.8), P("bbb", 690, 650, 40, 1.2), P("ccc", 705, 670, 41, 1.5)]
    from dataclasses import asdict
    json.dump([asdict(p) for p in pos], open("state/positions.json", "w"))
    json.dump({"peak": 1000.0, "current": 1000.0, "halted": False, "halted_on": None, "sessions_since_halt": 0, "history": []}, open("state/equity.json", "w"))
    book = quote_book(); calls = []
    def qf(ticker, expiry, strike):
        b = book.get((expiry.isoformat(), float(strike)))
        return None if b is None else OptionQuote(strike=float(strike), bid=b[0], ask=b[1], iv=0.2, open_interest=500)
    monkeypatch.setattr(data, "quote_for", qf); monkeypatch.setattr(data, "spot", lambda t: 699.0)
    monkeypatch.setattr(alerts, "status_alert", lambda w, title, lines, ok, dry: calls.append(("status", title, tuple(lines), ok)) or True)
    monkeypatch.setattr(alerts, "exit_alert", lambda w, p, debit, reason, pnl, dry: calls.append(("exit", p.id, round(debit, 6), reason, round(pnl, 6))) or True)
    monkeypatch.setattr(alerts, "entry_alert", lambda *a, **k: calls.append(("entry",)) or True)
    rc = mon.main()
    files = {n: open(os.path.join("state", n)).read() for n in sorted(os.listdir("state"))}
    return rc, calls, files


def test_guard_off_is_identical_to_baseline(tmp_path, monkeypatch):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    base = scenario(monitor_baseline, a, monkeypatch)
    monkeypatch.setenv("ASSIGNMENT_GUARD", "0")
    off = scenario(monitor, b, monkeypatch)
    assert off == base
    assert any(c[0] == "exit" and c[1] == "ccc" and c[3].startswith("stop loss") for c in base[1])   # the scenario really exercised an exit


def test_guard_on_only_adds_one_advisory_alert(tmp_path, monkeypatch):
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    base = scenario(monitor_baseline, a, monkeypatch)
    monkeypatch.delenv("ASSIGNMENT_GUARD", raising=False)
    on = scenario(monitor, b, monkeypatch)
    assert on[0] == base[0] and on[2] == base[2]                       # same return code, byte-identical positions.json / equity.json
    assert on[1][:len(base[1])] == base[1]                              # every baseline alert first, unchanged
    extra = on[1][len(base[1]):]
    assert len(extra) == 1 and extra[0][0] == "status" and extra[0][1] == "ASSIGNMENT RISK"
    assert len(extra[0][2]) == 1 and "[aaa]" in extra[0][2][0] and extra[0][2][0].startswith("ASSIGNMENT RISK: short leg ITM, extrinsic $0.02")
    assert not any("[bbb]" in l or "[ccc]" in l for l in extra[0][2])  # OTM and already-closed positions are not flagged
