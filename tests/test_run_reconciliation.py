"""ADR-016: 1回の実行の収支を検証する。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import run_reconciliation as rr


def test_balanced_run():
    acc = rr.account_run(["a", "b", "c"], ["a", "b"], {"c": "already_stored"})
    assert rr.is_balanced(acc)
    assert (acc.fetched, acc.written, acc.skipped) == (3, 2, 1)


def test_article_lost_without_reason_is_unaccounted():
    """エラーにならず、ログにも出ない消失を捕まえる。"""
    acc = rr.account_run(["a", "b", "c"], ["a"], {"b": "already_stored"})
    assert acc.unaccounted == ("c",)
    assert not rr.is_balanced(acc)


def test_skip_with_unknown_reason_is_flagged():
    """理由が書いてあっても、許可されていない理由なら説明になっていない。"""
    acc = rr.account_run(["a", "b"], ["a"], {"b": "whatever"})
    assert acc.unknown_reasons == ("b: whatever",)
    assert not rr.is_balanced(acc)


def test_duplicated_write_is_flagged():
    acc = rr.account_run(["a"], ["a", "a"], {})
    assert acc.duplicated_writes == ("a",)
    assert not rr.is_balanced(acc)


def test_write_that_was_never_fetched_is_unexpected():
    acc = rr.account_run(["a"], ["a", "z"], {})
    assert acc.unexpected == ("z",)
    assert not rr.is_balanced(acc)


def test_duplicate_in_feed_counts_once():
    """フィードが同じ記事を2回載せても、1件として扱う。"""
    acc = rr.account_run(["a", "a", "b"], ["a", "b"], {})
    assert acc.fetched == 2
    assert rr.is_balanced(acc)


def test_empty_run_is_balanced():
    assert rr.is_balanced(rr.account_run([], [], {}))


def test_log_fields_are_emitted_even_when_balanced():
    """無音では、合っていたのか確かめていないのか区別できない。"""
    fields = rr.build_log_fields(rr.account_run(["a"], ["a"], {}))
    assert fields["run_balanced"] is True
    assert fields["run_fetched"] == 1


def test_log_samples_are_limited():
    links = [f"l{i:02d}" for i in range(20)]
    fields = rr.build_log_fields(rr.account_run(links, [], {}))
    assert fields["run_unaccounted_count"] == 20
    assert len(fields["run_unaccounted_sample"]) == 5