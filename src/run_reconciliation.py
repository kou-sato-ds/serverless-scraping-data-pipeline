"""
1回の実行で取得した記事の行方を、1件残らず説明する。

ADR-016: 取得件数 = 書き込み件数 + 理由付きスキップ件数 を毎回確かめる。

背景:
    1回の実行では「取得 -> 書き込み or スキップ」が起きる。
    スキップには正当な理由がある(取得済み・ウォーターマーク外・中身が壊れている)。
    問題は、理由の無いまま消えた記事である。エラーにならず、ログにも出ない。

    本ファイルは実行ごとに収支を取る:
      - 取得したのに、書き込みにもスキップにも無い記事 -> unaccounted(説明できない差分)
      - 取得していないのに書き込まれた記事             -> unexpected
      - 同じ記事を2回書き込んだ                       -> duplicated_writes
      - 許可されていない理由でのスキップ              -> unknown_reasons

    姉妹プロジェクトの item 115(データ突合)を、1回の実行単位へ移植したもの。
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

ALLOWED_SKIP_REASONS = frozenset({"already_stored", "outside_watermark", "invalid_payload"})


@dataclass(frozen=True)
class RunAccount:
    fetched: int
    written: int
    skipped: int
    unaccounted: tuple[str, ...]
    unexpected: tuple[str, ...]
    duplicated_writes: tuple[str, ...]
    unknown_reasons: tuple[str, ...]


def account_run(fetched_links: list[str], written_links: list[str],
                skipped: dict[str, str]) -> RunAccount:
    """
    1回の実行の収支を取る。

    取得側の重複(フィードが同じ記事を2回載せる)は1件として数える。
    書き込み側の重複は、それ自体を問題として報告する。
    """
    fetched = set(fetched_links)
    written = set(written_links)
    dup = sorted(k for k, c in Counter(written_links).items() if c > 1)

    return RunAccount(
        fetched=len(fetched),
        written=len(written & fetched),
        skipped=len(set(skipped) & fetched),
        unaccounted=tuple(sorted(fetched - written - set(skipped))),
        unexpected=tuple(sorted(written - fetched)),
        duplicated_writes=tuple(dup),
        unknown_reasons=tuple(sorted(
            f"{link}: {reason}" for link, reason in skipped.items()
            if reason not in ALLOWED_SKIP_REASONS
        )),
    )


def is_balanced(acc: RunAccount) -> bool:
    """説明できない差分が1件も無いか。"""
    return (
        not acc.unaccounted
        and not acc.unexpected
        and not acc.duplicated_writes
        and not acc.unknown_reasons
        and acc.fetched == acc.written + acc.skipped
    )


def build_log_fields(acc: RunAccount) -> dict[str, object]:
    """
    Powertools Logger の extra へ渡すフィールド。

    収支が合っているときも出す。無音では「合っていた」のか
    「確かめていない」のか区別できない(ADR-010 と同じ判断)。
    例は先頭5件だけにする。全件をログに載せても誰も読めない。
    """
    return {
        "run_fetched": acc.fetched,
        "run_written": acc.written,
        "run_skipped": acc.skipped,
        "run_balanced": is_balanced(acc),
        "run_unaccounted_count": len(acc.unaccounted),
        "run_unaccounted_sample": list(acc.unaccounted[:5]),
        "run_unexpected_count": len(acc.unexpected),
        "run_duplicated_writes": list(acc.duplicated_writes[:5]),
        "run_unknown_reasons": list(acc.unknown_reasons[:5]),
    }