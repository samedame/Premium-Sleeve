"""Decide whether a scheduled GitHub Actions run should do any work.

Why this exists
---------------
The workflows used to drop runs unless the UTC hour matched an exact DST
offset. GitHub's cron is best-effort and, on this repo, starts runs 3.5-5
hours after the slot. Every delayed run landed in the "wrong" hour and
was skipped, so the entry scan never executed.

The guard now looks at what actually matters -- is the market in session
in New York, and has this job already done its work today -- instead of
the UTC hour. The workflows fire hourly; the guard lets through the first
run that lands in the window and drops the rest.

Rules
-----
* entry:   weekdays, 12:00-15:55 ET, at most once per ET date.
* monitor: weekdays, 09:45-15:55 ET, at most twice per ET date and at
           least 3 hours apart (monitor.py counts each run toward the
           kill-switch restart clock, so it must not run hourly).
* workflow_dispatch bypasses the window (the open-scheduler Worker and
  manual runs fire at a time of their choosing) but still counts toward
  the daily limit once it succeeds.

NYSE holidays are not modelled; a run on a holiday sees stale quotes
exactly as before.

Usage (from the workflows):
    python3 -m src.schedule_guard check  entry --event "$EVENT"
    python3 -m src.schedule_guard record entry
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
RUNS_PATH = "state/runs.json"
KEEP_DAYS = 14

RULES = {
    "entry": {"start": dt.time(12, 0), "end": dt.time(15, 55),
              "max_per_day": 1, "min_gap_hours": 0.0},
    "monitor": {"start": dt.time(9, 45), "end": dt.time(15, 55),
                "max_per_day": 2, "min_gap_hours": 3.0},
}


def load_runs(path: str = RUNS_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts).astimezone(dt.timezone.utc)


def decide(job: str, now_utc: dt.datetime, runs: dict,
           event: str = "schedule") -> tuple[bool, str]:
    """Return (run_it, reason)."""
    rule = RULES[job]
    now_et = now_utc.astimezone(ET)

    prior = [_parse(ts) for ts in runs.get(job, [])]
    today = [p for p in prior if p.astimezone(ET).date() == now_et.date()]

    if event != "schedule":
        return True, f"{event}: guard bypassed ({now_et:%a %H:%M} ET)"

    if now_et.weekday() >= 5:
        return False, f"{now_et:%a} is a weekend in ET"
    t = now_et.time()
    if not (rule["start"] <= t < rule["end"]):
        return False, (f"{now_et:%H:%M} ET is outside the {rule['start']:%H:%M}-"
                       f"{rule['end']:%H:%M} ET window")
    if len(today) >= rule["max_per_day"]:
        return False, f"{job} already ran {len(today)}x on {now_et.date()}"
    if today and rule["min_gap_hours"]:
        gap = (now_utc - max(today)).total_seconds() / 3600.0
        if gap < rule["min_gap_hours"]:
            return False, f"last {job} run was {gap:.1f}h ago (< {rule['min_gap_hours']:g}h)"
    return True, f"{now_et:%a %H:%M} ET is in window"


def record(job: str, now_utc: dt.datetime, runs: dict) -> dict:
    cutoff = now_utc - dt.timedelta(days=KEEP_DAYS)
    out = {}
    for name, stamps in runs.items():
        kept = [s for s in stamps if _parse(s) >= cutoff]
        if kept:
            out[name] = kept
    out.setdefault(job, []).append(now_utc.replace(microsecond=0).isoformat())
    return out


def _save(path: str, runs: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(runs, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="schedule_guard")
    ap.add_argument("action", choices=["check", "record"])
    ap.add_argument("job", choices=sorted(RULES))
    ap.add_argument("--event", default="schedule")
    ap.add_argument("--path", default=RUNS_PATH)
    args = ap.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    runs = load_runs(args.path)

    if args.action == "check":
        ok, reason = decide(args.job, now, runs, args.event)
        print(f"[guard] {args.job}: {'RUN' if ok else 'SKIP'} - {reason}")
        if not ok and os.environ.get("GITHUB_ENV"):
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as fh:
                fh.write("SKIP=1\n")
        return 0

    _save(args.path, record(args.job, now, runs))
    print(f"[guard] recorded {args.job} run at {now:%Y-%m-%dT%H:%MZ}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
