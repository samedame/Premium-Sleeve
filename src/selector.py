"""Pick the bull put spread.

Strikes are chosen by delta, never by dollar offset. There is no entry
signal: prior research found the RSI trigger added nothing over selling
the same spread on arbitrary days -- the edge is the variance risk
premium, not the timing. Candidates are therefore ranked on structural
quality (credit per dollar of risk), which is not a market-state bet.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .blackscholes import put_delta, vega, years_to_expiry
from .config import Config
from .data import Chain, OptionQuote


@dataclass
class Leg:
    strike: float
    delta: float
    vega_per_share: float
    bid: float
    ask: float
    iv: float
    open_interest: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0 if self.bid > 0 and self.ask > 0 else 0.0

    @property
    def width(self) -> float:
        return self.ask - self.bid if self.bid > 0 and self.ask > 0 else float("inf")


@dataclass
class Candidate:
    expiry: dt.date
    dte: int
    short: Leg
    long: Leg
    spot: float

    @property
    def width(self) -> float:
        return self.short.strike - self.long.strike

    @property
    def credit(self) -> float:
        """Credit per share at mid."""
        return self.short.mid - self.long.mid

    @property
    def max_loss_per_contract(self) -> float:
        return (self.width - self.credit) * 100.0

    @property
    def max_gain_per_contract(self) -> float:
        return self.credit * 100.0

    @property
    def net_vega_per_contract(self) -> float:
        """Negative: the spread is short vega. Dollars per vol point."""
        return (self.long.vega_per_share - self.short.vega_per_share) * 100.0

    @property
    def net_delta_per_contract(self) -> float:
        """Positive: a bull put spread is long delta."""
        return (self.long.delta - self.short.delta) * 100.0

    def roundtrip_cost(self, haircut: float) -> float:
        """Per share, open and close, at mid minus a fraction of width."""
        return haircut * (self.short.width + self.long.width) * 2.0

    @property
    def credit_to_risk(self) -> float:
        loss = self.max_loss_per_contract
        return self.max_gain_per_contract / loss if loss > 0 else 0.0


@dataclass
class Rejection:
    expiry: dt.date
    reason: str


def _to_leg(q: OptionQuote, spot_px: float, dte: int, r: float) -> Leg:
    t = years_to_expiry(dte)
    return Leg(
        strike=q.strike,
        delta=put_delta(spot_px, q.strike, t, r, q.iv),
        vega_per_share=vega(spot_px, q.strike, t, r, q.iv),
        bid=q.bid,
        ask=q.ask,
        iv=q.iv,
        open_interest=q.open_interest,
    )


def _nearest_by_delta(legs: list[Leg], target: float, tol: float) -> Leg | None:
    """Closest leg to |target| delta, or None if the miss exceeds tol."""
    best, best_err = None, float("inf")
    for leg in legs:
        err = abs(abs(leg.delta) - target)
        if err < best_err:
            best, best_err = leg, err
    if best is None or best_err > tol:
        return None
    return best


def build_candidates(
    chains: list[Chain], spot_px: float, cfg: Config
) -> tuple[list[Candidate], list[Rejection]]:
    """Return candidates passing the liquidity screen, plus rejections."""
    candidates: list[Candidate] = []
    rejected: list[Rejection] = []

    for chain in chains:
        legs = [_to_leg(q, spot_px, chain.dte, cfg.risk_free_rate) for q in chain.puts]
        legs = [l for l in legs if l.bid > 0 and l.ask > 0]
        if not legs:
            rejected.append(Rejection(chain.expiry, "no two-sided quotes"))
            continue

        short = _nearest_by_delta(legs, cfg.short_delta, cfg.delta_tolerance)
        if short is None:
            rejected.append(Rejection(chain.expiry, f"no strike near {cfg.short_delta:.2f} delta"))
            continue

        # Width cap binds before the delta target: restrict to strikes
        # within max_width, then take the one closest to the long-delta
        # target among those. When the cap binds, that is the lowest
        # strike allowed, i.e. the widest permitted spread.
        floor_strike = short.strike - cfg.max_width
        below = [l for l in legs if floor_strike <= l.strike < short.strike]
        if not below:
            rejected.append(
                Rejection(chain.expiry, f"no strike within {cfg.max_width:g} of {short.strike:g}")
            )
            continue
        long = _nearest_by_delta(below, cfg.long_delta, tol=1.0)
        if long is None:
            rejected.append(Rejection(chain.expiry, "no long leg available"))
            continue

        cand = Candidate(chain.expiry, chain.dte, short, long, spot_px)

        if cand.credit < cfg.min_credit:
            rejected.append(Rejection(chain.expiry, f"credit {cand.credit:.2f} below floor"))
            continue
        if min(short.open_interest, long.open_interest) < cfg.min_open_interest:
            rejected.append(
                Rejection(chain.expiry, f"open interest {min(short.open_interest, long.open_interest)} too thin")
            )
            continue
        cost = cand.roundtrip_cost(cfg.fill_haircut)
        if cost > cfg.max_roundtrip_cost_ratio * cand.credit:
            rejected.append(
                Rejection(
                    chain.expiry,
                    f"round trip {cost:.2f} exceeds {cfg.max_roundtrip_cost_ratio:.0%} of {cand.credit:.2f} credit",
                )
            )
            continue

        candidates.append(cand)

    candidates.sort(key=lambda c: c.credit_to_risk, reverse=True)
    return candidates, rejected
