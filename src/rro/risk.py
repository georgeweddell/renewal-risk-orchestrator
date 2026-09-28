"""Deterministic renewal risk scoring.

The agent gathers signals and explains them; this module turns them into a
score. Keeping the arithmetic out of the model makes the score reproducible,
auditable and easy to tune (config/risk.yaml) without touching a prompt.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

Band = Literal["healthy", "at-risk", "critical"]
# Stable codes for the factors, used to match an account against past decisions in memory.
Driver = Literal["usage_drop", "open_p1", "open_p2", "renewal_soon"]


class UsageRule(BaseModel):
    max_pct: float
    points: int
    label: str


class CountRule(BaseModel):
    points_each: int
    cap: int


class DaysRule(BaseModel):
    max_days: int
    points: int
    label: str


class BandRule(BaseModel):
    min_score: int
    band: Band


class RiskConfig(BaseModel):
    usage_change: list[UsageRule]
    open_p1: CountRule
    open_p2: CountRule
    days_to_renewal: list[DaysRule]
    bands: list[BandRule]

    @classmethod
    def load(cls, path: Path) -> RiskConfig:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


class RiskSignals(BaseModel):
    usage_pct_change: float = Field(description="WAU change, last 4 weeks vs prior 8, in percent")
    open_p1: int = Field(ge=0)
    open_p2: int = Field(ge=0)
    days_to_renewal: int


class Factor(BaseModel):
    code: Driver
    name: str
    evidence: str
    points: int
    rule: str | None = None


class RiskAssessment(BaseModel):
    score: int
    band: Band
    factors: list[Factor]
    drivers: list[Driver] = Field(description="Codes of the factors that scored points")
    signals: RiskSignals


def days_until(renewal: date, today: date) -> int:
    return (renewal - today).days


def score(signals: RiskSignals, config: RiskConfig) -> RiskAssessment:
    factors: list[Factor] = []

    usage_rule = next((r for r in config.usage_change if signals.usage_pct_change <= r.max_pct), None)
    factors.append(
        Factor(
            code="usage_drop",
            name="Usage trend",
            evidence=f"{signals.usage_pct_change:+.1f}% weekly active users (last 4 wks vs prior 8)",
            points=usage_rule.points if usage_rule else 0,
            rule=usage_rule.label if usage_rule else None,
        )
    )

    for code, label, count, rule in (
        ("open_p1", "Open P1 issues", signals.open_p1, config.open_p1),
        ("open_p2", "Open P2 issues", signals.open_p2, config.open_p2),
    ):
        points = min(count * rule.points_each, rule.cap)
        factors.append(
            Factor(
                code=code,
                name=label,
                evidence=f"{count} open",
                points=points,
                rule=f"{rule.points_each} pts each, max {rule.cap}" if points else None,
            )
        )

    days = signals.days_to_renewal
    days_rule = next((r for r in config.days_to_renewal if days <= r.max_days), None)
    factors.append(
        Factor(
            code="renewal_soon",
            name="Time to renewal",
            evidence=f"{days} days" if days >= 0 else f"renewal date passed {-days} days ago",
            points=days_rule.points if days_rule else 0,
            rule=days_rule.label if days_rule else None,
        )
    )

    total = min(sum(f.points for f in factors), 100)
    band = next(b.band for b in config.bands if total >= b.min_score)
    drivers = [f.code for f in factors if f.points > 0]
    return RiskAssessment(score=total, band=band, factors=factors, drivers=drivers, signals=signals)
