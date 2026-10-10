"""ADR-015: 日本時間の1日と UTC パーティションの対応を検証する。"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import jst_partitions as jp


def test_jst_day_spans_two_utc_partitions():
    """1つしか読まなければ、9時間分の記事が漏れる。"""
    assert jp.utc_partitions_for_jst_day(date(2026, 10, 10)) == ["dt=2026-10-09", "dt=2026-10-10"]


def test_bounds_are_shifted_by_nine_hours():
    start, end = jp.utc_bounds_for_jst_day(date(2026, 10, 10))
    assert start == datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)


def test_early_morning_article_belongs_to_that_jst_day():
    """UTC では前日でも、日本時間ではその日の記事。"""
    ts = datetime(2026, 10, 9, 23, 30, tzinfo=timezone.utc)
    assert jp.jst_day_of(ts) == date(2026, 10, 10)


def test_early_morning_article_lives_in_the_previous_utc_partition():
    ts = datetime(2026, 10, 9, 23, 30, tzinfo=timezone.utc)
    assert jp.PARTITION_FORMAT.format(ts) == "dt=2026-10-09"
    assert "dt=2026-10-09" in jp.utc_partitions_for_jst_day(jp.jst_day_of(ts))


def test_naive_datetime_is_rejected():
    """UTC か JST か分からない時刻が、9時間ずれの元凶。"""
    with pytest.raises(ValueError, match="naive"):
        jp.jst_day_of(datetime(2026, 10, 10, 8, 0))


def test_month_boundary_is_handled():
    assert jp.utc_partitions_for_jst_day(date(2026, 11, 1)) == ["dt=2026-10-31", "dt=2026-11-01"]


def test_read_plan_has_partitions_and_exact_bounds():
    """パーティションで絞り、時刻で正確に切る。片方だけでは足りない。"""
    plan = jp.read_plan(date(2026, 10, 10))
    assert plan["partitions"] == ["dt=2026-10-09", "dt=2026-10-10"]
    assert plan["start_utc"] == "2026-10-09T15:00:00+00:00"
    assert plan["end_utc"] == "2026-10-10T15:00:00+00:00"


def test_last_instant_of_the_jst_day_is_inside_the_second_partition():
    ts = datetime(2026, 10, 10, 14, 59, 59, 999999, tzinfo=timezone.utc)
    assert jp.jst_day_of(ts) == date(2026, 10, 10)
    assert jp.PARTITION_FORMAT.format(ts) == "dt=2026-10-10"