"""
ADR-013: 再生計画のテスト。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import backfill

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _dt(day, hour):
    return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)


class TestValidation:
    def test_reversed_window_is_rejected(self):
        errors = backfill.validate_window(_dt(21, 10), _dt(21, 5), NOW)
        assert any("after end" in e for e in errors)

    def test_future_window_is_rejected(self):
        errors = backfill.validate_window(_dt(22, 10), _dt(23, 10), NOW)
        assert any("future" in e for e in errors)

    def test_excessive_window_is_rejected(self):
        errors = backfill.validate_window(_dt(1, 0), _dt(20, 0), NOW)
        assert any("exceeding" in e for e in errors)

    def test_naive_datetime_is_rejected(self):
        """タイムゾーン無しの時刻は、UTC と JST の取り違えを生む。"""
        naive = datetime(2026, 9, 21, 10)
        errors = backfill.validate_window(naive, naive, NOW)
        assert any("timezone-aware" in e for e in errors)

    def test_valid_window_passes(self):
        assert backfill.validate_window(_dt(21, 0), _dt(21, 5), NOW) == []


class TestSlots:
    def test_slots_are_hourly_and_inclusive(self):
        slots = backfill.build_hourly_slots(_dt(21, 0), _dt(21, 3))
        assert len(slots) == 4
        assert slots[0] == _dt(21, 0)
        assert slots[-1] == _dt(21, 3)

    def test_minutes_are_truncated(self):
        """スケジュールは毎時0分。ずれると元の実行と同じ fetched_at にならない。"""
        start = datetime(2026, 9, 21, 10, 37, tzinfo=timezone.utc)
        slots = backfill.build_hourly_slots(start, start)
        assert slots[0].minute == 0


class TestReplayEvent:
    def test_event_has_eventbridge_shape(self):
        event = backfill.build_replay_event(_dt(21, 10))
        assert event["time"] == "2026-09-21T10:00:00Z"
        assert event["id"].startswith("replay-")

    def test_same_slot_yields_same_id(self):
        """同じ時間帯の再生を、ADR-005 の相関IDで束ねられること。"""
        a = backfill.build_replay_event(_dt(21, 10))
        b = backfill.build_replay_event(_dt(21, 10))
        assert a["id"] == b["id"]

    def test_different_slots_yield_different_ids(self):
        a = backfill.build_replay_event(_dt(21, 10))
        b = backfill.build_replay_event(_dt(21, 11))
        assert a["id"] != b["id"]


class TestPlan:
    def test_completed_slots_are_skipped(self):
        plan = backfill.build_replay_plan(
            _dt(21, 0), _dt(21, 2), NOW, completed={"2026-09-21T00"}
        )
        assert plan["total_slots"] == 3
        assert plan["skipped"] == 1
        assert len(plan["events"]) == 2

    def test_batches_respect_concurrency(self):
        plan = backfill.build_replay_plan(_dt(21, 0), _dt(21, 9), NOW)
        assert all(len(b) <= backfill.MAX_CONCURRENT_REPLAYS for b in plan["batches"])
        assert sum(len(b) for b in plan["batches"]) == 10

    def test_invalid_plan_produces_no_events(self):
        plan = backfill.build_replay_plan(_dt(21, 10), _dt(21, 5), NOW)
        assert plan["valid"] is False
        assert plan["events"] == []