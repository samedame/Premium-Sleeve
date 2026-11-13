# premium-sleeve

One short-premium position on SPY, properly sized, behind one gate.

This repo replaces four bots that were all selling the same SPY/QQQ
premium — options scanner S1, options scanner S2, `spy-bps-bot`, and the
condor leg of `daytrading_bot`. They were not four strategies. They were
one trade in four wrappers, and because each sized itself in isolation,
their vega stacked with no shared limit. Consolidating is the whole
point: every risk check here applies to the book, which is what four
separate repos could not do.

**Alert-only.** Robinhood has no trading API. Every alert is an
instruction to you, not an order.

## Why this and not the switcher

The conditional multi-strategy allocator came back NO-GO. Two reasons
carried it:

- The premium being timed may be close to zero now. Dew-Becker & Giglio
  (June 2026) date a break at August 2012, after which standard options
  strategies no longer show statistically significant alphas.
- The test could not have confirmed an edge anyway. Over a ~5.75-year
  holdout with strategies correlated ~0.85, the Sharpe difference has to
  exceed ~0.45 before its confidence interval clears zero. Realistic
  timing gains are 0.1–0.2.

Phase 0 — the free Cboe-index feasibility test — was never run. The
verdict rests on literature and arithmetic, not a backtest. If you want
the gate resolved with evidence, that test is still the cheap way to do
it, and nothing in this repo forecloses it.

**No number in this repo is backtested.** yfinance is a live-pricing
source with no point-in-time history and no historical bid/ask. The
figures below come from live chains on 2026-10-27, not from a simulation.

## The trade

| | |
|---|---|
| Structure | Bull put spread on SPY, defined risk (Level 3 legal) |
| Short leg | nearest to 0.30 delta |
| Long leg | nearest to 0.10 delta, **capped at 10 points wide** |
| Entry | 30–45 DTE, fixed-calendar, **no timing signal** |
| Exits | 50% of credit captured · 2× credit stop · time exit at 21 DTE |
| Concurrency | max 3 positions, expiries ≥7 days apart |

There is no entry signal on purpose. Your own research found the RSI
trigger added nothing over selling the same spread on arbitrary days:
the edge was the variance risk premium, not the timing.

### The width cap, and why it exists

At SPY ~770 a true 30Δ/10Δ spread runs about **36 points wide — $3,151
max loss per contract**. Against the 5% per-position cap that needs
~$63,000 before one contract is legal. The research's "$15k floor"
assumed a 5-wide spread and was never re-checked at current index
levels.

So width binds before the delta target. The long leg moves closer in
(a higher delta than 0.10), buying capital efficiency at the cost of a
smaller credit. Raising `max_width` widens the spread, increases the
credit, and raises the capital floor.

## Capital

Measured against live chains on 2026-10-27 (756/746P, $1.89 credit,
$811 max loss/contract, −11.6 vega/point):

| Capital | Contracts | Binding limit |
|---|---|---|
| $1k–$15k | **0** | per-position risk cap |
| $20k | 1 | per-position risk cap |
| $25k | 1 | per-position risk cap |
| $50k | 3 | per-position risk cap |
| $100k | 6 | per-position risk cap |

**Below $20,000 this sleeve cannot open a position at all.** That is the
honest floor at today's SPY level, and it is higher than the $15k the
research estimated. It moves with the index: a higher SPY raises it.

`ACCOUNT_CAPITAL_FOR_SIZING` is unset by default. With no value the bot
runs **UNSIZED** — it shows the trade and refuses to name a contract
count. That is deliberate. Set it when you have decided the number, and
not before.

## Risk layer

| Limit | Value |
|---|---|
| Per position | 5% of capital at defined risk |
| Book | 15% of capital at defined risk |
| **Book vega** | **0.15% of capital per vol point** |
| Kill switch | halt at −15% from sleeve peak |
| Restart | 20 trading sessions **and** VIX/VIX3M < 1.00 |

The vega budget is the limit that matters most for a short-premium book
and the one none of the four predecessor bots had.

## The gate

`VIX/VIX3M ≥ 1.00` (backwardation) blocks new entries. It never
force-closes an open position — exits are governed by the position's own
profit target, stop and time rule.

Your existing vol-regime-gate lives in local `research 3` and can't be
imported from Actions, so `src/gate.py` reimplements the same single
rule. To hand control back to yours, have it write:

```json
{ "allow_entries": false, "reason": "regime gate: backwardation" }
```

to `state/gate_override.json`. An override file always wins.

## Running it

```bash
pip install -r requirements.txt
DRY_RUN=1 python -m src.entry     # print alerts, touch nothing
DRY_RUN=1 python -m src.monitor
python3 tests/test_sleeve.py      # no network needed
```

Two workflows, one trigger each:

| Workflow | Trigger | Notes |
|---|---|---|
| `entry.yml` | `open-scheduler` (Cloudflare Worker) at **15:40 ET** | One scan per weekday. Its hourly GitHub cron was removed on 2026-11-13: it could not be timed. A run landing inside the window before 15:40 would have done the scan early and the Worker's dispatch (which bypasses the once-a-day limit) would then have run a second one; in practice every cron run landed after the close and only burned Actions minutes. |
| `monitor.yml` | hourly GitHub cron, filtered by `src/schedule_guard.py` | The Worker has no monitor job, so the cron stays. The guard lets through at most two runs per ET date, at least 3h apart, inside 09:45-15:55 ET, tracked in `state/runs.json`. |

GitHub cron starts runs hours late on this repo, which is why the guard keys on New York time and a daily
limit instead of an exact UTC hour. `workflow_dispatch` bypasses the window on both workflows.

### Why `state/positions.json` is still empty

Checked on 2026-11-13. Three things, in order of weight:

1. **At $1,500 the sleeve sizes to zero contracts.** `ACCOUNT_CAPITAL_FOR_SIZING` defaults to 1500 in the
   workflows. The per-position cap is 5% of capital ($75) and one 10-wide spread risks about $811, so every
   candidate ends in `NO ENTRY: sizes to zero contracts: per-position risk cap is binding` (see the capital
   table above: the first contract fits near $20,000). This is the sleeve working as designed, and it means no
   position can open at this account size.
2. **No real entry scan has completed.** The Worker's 15:40 ET dispatches began on 2026-11-09 and every one was
   refused by the GitHub Actions billing lock-out (9, 10 and 12 Nov failed in seconds; 11 Nov did not start).
3. **Before that, only the cron ran**, hours late, and the guard correctly skipped it as outside the window.

The stop rule is already written (see "Risk layer"): entries halt at a 15% drawdown from the sleeve's peak and
restart only after 20 sessions with VIX/VIX3M below 1.00.

Set `DISCORD_WEBHOOK_URL` as a repo **secret**;
`ACCOUNT_CAPITAL_FOR_SIZING` and `ASSUME_FILLED` as repo **variables**.

### BotDeck

State is committed back to `state/` and the push webhook carries it to
`botdeck-theta.vercel.app/api/webhooks/github`. There is no POST
endpoint and this bot does not invent one — point that repo webhook at
BotDeck and the commits do the rest. `state/positions.json` holds the
book, `state/equity.json` the realized curve and kill-switch status.

### Fills

`ASSUME_FILLED=true` records an alerted entry as filled at the alerted
credit. That is a modelling convenience, not a fact — you are filling
these by hand and your real fills will differ. Set it `false` to confirm
manually by editing `state/positions.json`. Monitoring prices the exit
at mid plus a 25%-of-width haircut; mid fills on spreads are fiction.

## Decommissioning the four

Do this in order, and do not run the old bots alongside this one — that
reintroduces the exact stacking problem it was built to remove.

1. Let every open position in S1, S2, `spy-bps-bot` and the condor leg
   of `daytrading_bot` close on its own rules. Do not force them.
2. Disable each one's schedule (comment out `on.schedule`), leaving
   `workflow_dispatch` so you can still inspect them.
3. Start this sleeve UNSIZED and watch the alerts for a cycle before
   setting `ACCOUNT_CAPITAL_FOR_SIZING`.
4. Keep the pre-earnings straddle as-is. It is long vol — the one sleeve
   that is genuinely not this trade.

## What this is not

It does not time entries. It does not switch strategies. It does not
pretend yfinance is a backtesting source. It is one exposure, sized once,
with a vega budget and a kill switch — which the research found was the
only improvement actually available.

## Known gaps

- Phase 0 was never run; the NO-GO rests on literature and arithmetic.
- No backtest exists here. Nothing in this repo validates the parameters.
- The $20k floor is computed from one day's chains and moves with SPY.
- yfinance rate-limits unpredictably; `src/data.py` retries with backoff
  and raises rather than returning empty, so a throttled run fails loudly
  instead of looking like "no candidates".
- Early assignment and pin risk on the short leg are not modelled.

## Data source (Tradier first, labelled yfinance fallback)

`src/data.py` asks Tradier first (`TRADIER_API_KEY`, optional `TRADIER_BASE_URL`; read-only client in `src/tradier.py`) and
falls back to yfinance if the key is missing or Tradier fails. Every ENTRY/EXIT alert has a **Data source** field; yfinance
data is always labelled **DELAYED/UNVERIFIED**. If a leg has `bid <= 0`, `ask <= 0`, or (Tradier) a quote older than
`data.MAX_QUOTE_AGE_MIN`, the bot posts a **NOTICE** instead of an entry alert, and the monitor reports the position as
UNPRICED instead of evaluating exits on a bad price. Strike selection (`src/selector.py`) is unchanged.
`python3 tests/test_data_sources.py` checks this offline, including identical strike selection from both sources.
