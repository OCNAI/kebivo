"""The burst acceptance gate rejects unexpected response statuses."""

import runpy
from collections import Counter
from pathlib import Path

script = Path(__file__).resolve().parents[2] / "scripts/feedback-burst.py"
burst_verdict = runpy.run_path(str(script))["burst_verdict"]


def test_burst_verdict_rejects_unauthorized_responses() -> None:
    passed, offending = burst_verdict(Counter({401: 100}), 0)
    assert not passed
    assert offending == {"401": 100}


def test_burst_verdict_accepts_persisted_successes() -> None:
    assert burst_verdict(Counter({202: 100}), 100) == (True, {})


def test_burst_verdict_requires_a_success() -> None:
    assert burst_verdict(Counter({503: 100}), 0) == (False, {})
