"""Configuration for the consolidated short-premium sleeve.

Every risk number here traces to the research verdict that replaced the
switcher. Changing one changes the sleeve's risk profile -- do not tune
these casually, and never tune them to make a recent loss look better.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Config:
    # --- underlying -------------------------------------------------
    underlying: str = "SPY"

    # --- structure: bull put spread, strikes chosen by delta --------
    short_delta: float = 0.30
    long_delta: float = 0.10
    delta_tolerance: float = 0.07   # widest |delta| miss we will accept
    # Width cap. At SPY ~770 a true 30d/10d spread runs ~36 points wide
    # ($3,151 max loss), which the 5% per-position cap will not clear
    # below ~$63k of capital. Capping width moves the long leg closer in
    # -- a higher long delta than 0.10 -- buying capital efficiency at
    # the cost of a smaller credit. Width binds before the delta target.
    max_width: float = 10.0
    dte_min: int = 30
    dte_max: int = 45

    # --- exits ------------------------------------------------------
    profit_target_pct: float = 0.50   # close at 50% of credit captured
    stop_loss_multiple: float = 2.0   # close if debit >= 2x credit received
    time_exit_dte: int = 21           # close at 21 DTE regardless

    # --- book limits ------------------------------------------------
    max_concurrent: int = 3
    stagger_min_days: int = 7         # new expiry must be >=7d from any open one
    max_risk_per_position_pct: float = 0.05   # 5% of capital at defined risk
    max_book_risk_pct: float = 0.15           # 15% of capital across the book
    # Book vega cap: dollars of P/L per 1 vol point, as a fraction of capital.
    max_book_vega_pct: float = 0.0015         # 0.15% of capital per vol point

    # --- kill switch ------------------------------------------------
    kill_drawdown_pct: float = 0.15           # halt at -15% from sleeve peak
    kill_restart_sessions: int = 20           # and wait 20 trading sessions
    kill_restart_max_ratio: float = 1.00      # and VIX/VIX3M must be < 1.00

    # --- regime gate ------------------------------------------------
    # Backwardation (VIX >= VIX3M) blocks new entries. This is the only
    # on/off switch in front of the sleeve.
    gate_ratio_threshold: float = 1.00
    gate_external_path: str = "state/gate_override.json"

    # --- liquidity screen (pre-registered, applied before any entry) -
    # Reject if round-trip cost exceeds this fraction of the credit.
    max_roundtrip_cost_ratio: float = 0.25
    min_open_interest: int = 100
    min_credit: float = 0.15          # dollars per share; below this, skip

    # --- sizing -----------------------------------------------------
    # Deliberately unset by default. With no value the bot alerts in
    # UNSIZED mode: it shows the trade but refuses to name a contract
    # count. Research put the floor for a multi-position book at ~$15k.
    account_capital: float | None = field(
        default_factory=lambda: _env_float("ACCOUNT_CAPITAL_FOR_SIZING", None)
    )
    capital_floor_warn: float = 15000.0

    # --- execution assumptions -------------------------------------
    # Alert-only: Robinhood has no trading API. When True, an alerted
    # entry is recorded as filled at the alerted credit. Set False to
    # require manual confirmation by editing state/positions.json.
    assume_filled: bool = field(
        default_factory=lambda: _env_bool("ASSUME_FILLED", True)
    )
    # Fills are modelled at mid minus this fraction of the bid/ask width.
    fill_haircut: float = 0.25

    risk_free_rate: float = 0.04

    # --- plumbing ---------------------------------------------------
    discord_webhook: str = field(
        default_factory=lambda: os.environ.get("DISCORD_WEBHOOK_URL", "")
    )
    positions_path: str = "state/positions.json"
    equity_path: str = "state/equity.json"
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))

    def validate(self) -> None:
        if not 0 < self.long_delta < self.short_delta < 1:
            raise ValueError("require 0 < long_delta < short_delta < 1")
        if self.dte_min >= self.dte_max:
            raise ValueError("dte_min must be below dte_max")
        if self.time_exit_dte >= self.dte_min:
            raise ValueError("time_exit_dte must be below dte_min")
        if self.max_risk_per_position_pct * 1 > self.max_book_risk_pct:
            raise ValueError("per-position risk cap exceeds book cap")
        if self.account_capital is not None and self.account_capital <= 0:
            raise ValueError("ACCOUNT_CAPITAL_FOR_SIZING must be positive")


def load() -> Config:
    cfg = Config()
    cfg.validate()
    return cfg
