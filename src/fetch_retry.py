"""
RSS 取得の再試行を、Lambda の残り時間の内側に収める。

ADR-017: 一時的な失敗だけを指数バックオフで再試行し、締切は残り時間から決める。

背景:
    ADR-003 で「失敗したら例外を投げ、Lambda の非同期リトライと DLQ に任せる」と決めた。
    しかし Lambda の再試行は固定間隔(1分後・2分後)であり、
    一瞬のネットワーク不調にも1分待つことになる。

    そこで関数の中で短く再試行する。ただし2つを守る:
      - 一時的な失敗(タイムアウト・429・5xx)だけを再試行する
      - 締切は固定値ではなく Lambda の残り時間から決める。
        タイムアウトで強制終了されると、例外もログも残らず ADR-003 の DLQ に繋がらない。
        残り時間が足りなければ、待たずに例外を投げて ADR-003 に引き渡す。

    姉妹プロジェクトの item 116 を、Lambda の実行時間制約に合わせて移植したもの。
"""
from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# 例外を投げてログを書き、ADR-003 の経路へ渡すために残しておく時間。
SAFETY_MARGIN_MS = 5000


class TransientFetchError(Exception):
    """時間を置けば直る見込みのある取得失敗。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def is_retryable(error: BaseException) -> bool:
    """
    再試行すべきかを判定する。

    判定できないものは再試行しない。原因不明のまま繰り返せば、
    残り時間を使い切ってから ADR-003 に渡ることになる。
    """
    if isinstance(error, TimeoutError):
        return True
    status = getattr(error, "status", None)
    if status is not None:
        return status in RETRYABLE_STATUS
    return isinstance(error, TransientFetchError)


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 8.0,
                  rng: random.Random | None = None) -> float:
    """attempt 回目(0始まり)の待ち時間。0〜min(cap, base*2^attempt) の一様乱数(フルジッター)。"""
    ceiling = min(cap, base * (2 ** attempt))
    return (rng or random).uniform(0, ceiling)


def usable_seconds(remaining_ms: int, safety_margin_ms: int = SAFETY_MARGIN_MS) -> float:
    """待ちに使ってよい秒数。安全余白を差し引き、負にはしない。"""
    return max(remaining_ms - safety_margin_ms, 0) / 1000


def retry_fetch(fn: Callable[[], T], remaining_ms: Callable[[], int],
                max_attempts: int = 4, base: float = 0.5, cap: float = 8.0,
                sleep: Callable[[float], None] = time.sleep,
                rng: random.Random | None = None) -> tuple[T, int]:
    """
    fn を再試行付きで呼び、(結果, 試行回数) を返す。

    remaining_ms には Lambda の context.get_remaining_time_in_millis を渡す。
    次の待ちが残り時間(安全余白を除く)を超えるなら、待たずに最後の例外を投げる。
    """
    for attempt in range(max_attempts):
        try:
            return fn(), attempt + 1
        except Exception as error:
            if not is_retryable(error) or attempt == max_attempts - 1:
                raise
            delay = backoff_delay(attempt, base, cap, rng)
            if delay > usable_seconds(remaining_ms()):
                raise
            sleep(delay)
    raise RuntimeError("unreachable")