"""
app.py と fetch_retry の接続を検証する (ADR-017 / ADR-018)。

feedparser.parse() は通信に失敗しても例外を投げない。
ここでは「一時的な失敗だけが例外に戻り、残り時間の内側で再試行される」ことを固定する。
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.error import URLError

import pytest

import app
import fetch_retry

EVENT = {"id": "test-event-id", "time": "2026-10-17T00:00:00Z"}


def _feed(**kwargs: object) -> SimpleNamespace:
    base: dict[str, object] = {"bozo": 0, "bozo_exception": None, "entries": []}
    base.update(kwargs)
    return SimpleNamespace(**base)


class FakeContext:
    """Powertools が読む属性と、残り時間だけを持つ偽の Lambda context。"""
    function_name = "rss-fetcher"
    memory_limit_in_mb = 128
    invoked_function_arn = "arn:aws:lambda:ap-northeast-1:123456789012:function:rss-fetcher"
    aws_request_id = "test-request-id"

    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


# --- raise_if_transient: 何を例外に戻すか ---------------------------------

@pytest.mark.parametrize("status", [429, 503])
def test_retryable_http_status_becomes_transient_error(status: int) -> None:
    with pytest.raises(fetch_retry.TransientFetchError) as info:
        app.raise_if_transient(_feed(status=status))
    assert info.value.status == status
    assert fetch_retry.is_retryable(info.value)


def test_network_failure_without_entries_is_transient() -> None:
    """通信失敗が 204「ニュースが無かった」として記録されるのを防ぐ。"""
    feed = _feed(bozo=1, bozo_exception=URLError("timed out"))
    with pytest.raises(fetch_retry.TransientFetchError):
        app.raise_if_transient(feed)


def test_parse_warning_with_entries_is_not_raised() -> None:
    """記事が取れているなら、XML の軽い崩れで時間を使わない。"""
    feed = _feed(bozo=1, bozo_exception=ValueError("mismatched tag"), entries=[object()])
    app.raise_if_transient(feed)


def test_network_error_with_entries_is_not_raised() -> None:
    feed = _feed(bozo=1, bozo_exception=URLError("reset"), entries=[object()])
    app.raise_if_transient(feed)


def test_not_found_keeps_current_behaviour() -> None:
    """404 は再試行しても直らない。扱いは別 PR で決めるため、本 PR では変えない。"""
    app.raise_if_transient(_feed(status=404))


# --- lambda_handler: 残り時間の内側で再試行するか -------------------------

def test_handler_retries_a_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def flaky() -> list[dict]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise fetch_retry.TransientFetchError("503", status=503)
        return []

    monkeypatch.setattr(app, "fetch_articles", flaky)
    monkeypatch.setattr(fetch_retry, "backoff_delay", lambda *a, **k: 0.0)
    result = app.lambda_handler(EVENT, FakeContext(remaining_ms=60_000))
    assert calls["n"] == 2
    assert result["statusCode"] == 204


def test_handler_does_not_wait_without_time_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """残り時間が安全余白より短ければ、待たずに ADR-003 (DLQ) へ渡す。"""
    calls = {"n": 0}

    def failing() -> list[dict]:
        calls["n"] += 1
        raise fetch_retry.TransientFetchError("503", status=503)

    monkeypatch.setattr(app, "fetch_articles", failing)
    monkeypatch.setattr(fetch_retry, "backoff_delay", lambda *a, **k: 0.1)
    with pytest.raises(fetch_retry.TransientFetchError):
        app.lambda_handler(EVENT, FakeContext(remaining_ms=1_000))
    assert calls["n"] == 1


def test_handler_does_not_retry_permanent_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """原因不明のまま繰り返せば、残り時間を使い切ってから DLQ に渡ることになる。"""
    calls = {"n": 0}

    def broken() -> list[dict]:
        calls["n"] += 1
        raise ValueError("unexpected")

    monkeypatch.setattr(app, "fetch_articles", broken)
    with pytest.raises(ValueError):
        app.lambda_handler(EVENT, FakeContext(remaining_ms=60_000))
    assert calls["n"] == 1