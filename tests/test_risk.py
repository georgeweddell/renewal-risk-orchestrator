from datetime import date

import pytest

from rro.risk import RiskConfig, RiskSignals, days_until, score
from rro.settings import PROJECT_ROOT


@pytest.fixture(scope="module")
def config() -> RiskConfig:
    return RiskConfig.load(PROJECT_ROOT / "config" / "risk.yaml")


def signals(usage=0.0, p1=0, p2=0, days=365) -> RiskSignals:
    return RiskSignals(usage_pct_change=usage, open_p1=p1, open_p2=p2, days_to_renewal=days)


def points(assessment, factor_name: str) -> int:
    return next(f.points for f in assessment.factors if f.name == factor_name)


@pytest.mark.parametrize(
    ("usage", "expected"),
    [(15.0, 0), (-9.9, 0), (-10.0, 10), (-19.9, 10), (-20.0, 25), (-39.9, 25), (-40.0, 40), (-90.0, 40)],
)
def test_usage_thresholds(config, usage, expected):
    assert points(score(signals(usage=usage), config), "Usage trend") == expected


def test_ticket_points_are_capped(config):
    result = score(signals(p1=5, p2=7), config)
    assert points(result, "Open P1 issues") == 30
    assert points(result, "Open P2 issues") == 10


@pytest.mark.parametrize(("days", "expected"), [(-3, 20), (0, 20), (29, 20), (30, 12), (59, 12), (60, 6), (89, 6), (90, 0)])
def test_days_to_renewal(config, days, expected):
    assert points(score(signals(days=days), config), "Time to renewal") == expected


@pytest.mark.parametrize(("sig", "band"), [
    (signals(), "healthy"),
    (signals(p1=2, days=100), "healthy"),     # 30
    (signals(p1=2, p2=1, days=100), "at-risk"),  # 35: boundary
    (signals(usage=-45, p1=1, days=100), "at-risk"),  # 55
    (signals(usage=-45, p1=1, days=60), "at-risk"),   # 61
    (signals(usage=-45, p1=1, days=59), "critical"),  # 67
])  # fmt: skip
def test_bands(config, sig, band):
    assert score(sig, config).band == band


def test_score_explains_itself(config):
    result = score(signals(usage=-45, p1=2, days=20), config)
    assert result.score == 90
    assert [f.name for f in result.factors] == ["Usage trend", "Open P1 issues", "Open P2 issues", "Time to renewal"]
    assert sum(f.points for f in result.factors) == result.score


def test_days_until():
    assert days_until(date(2026, 10, 26), date(2026, 9, 28)) == 28
