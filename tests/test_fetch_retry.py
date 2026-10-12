"""ADR-017: Lambda の残り時間の内側で再試行することを検証する。実際には眠らない。"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import fetch_retry as fr


class Flaky:
    def __init__(self, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "feed"


def plenty() -> int:
    return 60_000


def test_transient_failure_is_retried_until_success():
    fn = Flaky(2, fr.TransientFetchError("reset"))
    result, attempts = fr.retry_fetch(fn, plenty, sleep=lambda s: None, rng=random.Random(0))
    assert result == "feed"
    assert attempts == 3


def test_timeout_is_retryable():
    assert fr.is_retryable(TimeoutError())


@pytest.mark.parametrize("status,expected", [(429, True), (503, True), (404, False), (403, False)])
def test_http_status_classification(status, expected):
    assert fr.is_retryable(fr.TransientFetchError("x", status=status)) is expected


def test_unknown_error_is_not_retried():
    """原因不明のまま繰り返せば、残り時間を使い切ってから ADR-003 に渡ることになる。"""
    fn = Flaky(5, ValueError("parse"))
    with pytest.raises(ValueError):
        fr.retry_fetch(fn, plenty, sleep=lambda s: None)
    assert fn.calls == 1


def test_gives_up_when_remaining_time_is_short():
    """強制終了されれば、例外もログも残らず DLQ に繋がらない。"""
    fn = Flaky(5, fr.TransientFetchError("down"))
    slept: list[float] = []
    with pytest.raises(fr.TransientFetchError):
        fr.retry_fetch(fn, lambda: 5_500, base=10, cap=10, sleep=slept.append, rng=random.Random(1))
    assert sum(slept) <= 0.5


def test_safety_margin_is_reserved():
    assert fr.usable_seconds(15_000) == 10.0
    assert fr.usable_seconds(3_000) == 0.0


def test_gives_up_after_max_attempts():
    fn = Flaky(10, fr.TransientFetchError("down"))
    with pytest.raises(fr.TransientFetchError):
        fr.retry_fetch(fn, plenty, max_attempts=3, sleep=lambda s: None, rng=random.Random(0))
    assert fn.calls == 3


def test_jitter_stays_within_the_ceiling():
    rng = random.Random(42)
    for n in range(6):
        assert 0 <= fr.backoff_delay(n, base=0.5, cap=8.0, rng=rng) <= min(8.0, 0.5 * 2 ** n)