"""
失敗した時間帯の再実行（リプレイ）計画。

ADR-013: バックフィルを「失敗した時間帯の再生」として設計する。

背景:
    Google News RSS は「今」の記事しか返さない。過去のフィードは取り直せない。
    したがって本パイプラインのバックフィルは、
    **失敗した時間帯の Lambda 実行を、同じイベントで再生する**ことを意味する。

    これが安全に行えるのは 2 つの設計のおかげである:
      - ADR-002: S3 キーが記事内容から決定的に導出される -> 再実行しても重複しない
      - ADR-005: 相関IDが event['id'] -> 同じIDで再生すれば元の試行と束ねて追跡できる

    姉妹プロジェクトの item 103 (GCP側のバックフィル計画) を AWS 側へ移植する。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

# 再生できる最大時間数。誤って1年分を指定する事故を防ぐ。
MAX_REPLAY_HOURS = 72

# 同時に投げる実行数。Lambda の同時実行枠を本番スケジュールと奪い合わないため。
MAX_CONCURRENT_REPLAYS = 3


def validate_window(start: datetime, end: datetime, now: datetime) -> list[str]:
    """
    再生範囲の妥当性を検証する。

    リプレイは大量の実行を生む操作であり、指定ミスの代償が大きい。
    実行前に弾くのが唯一の防御になる。
    """
    errors = []

    if start.tzinfo is None or end.tzinfo is None:
        errors.append("start and end must be timezone-aware (UTC)")
        return errors

    if start > end:
        errors.append(f"start {start.isoformat()} is after end {end.isoformat()}")

    if end > now:
        errors.append("end is in the future; there is nothing to replay")

    hours = int((end - start).total_seconds() // 3600) + 1
    if hours > MAX_REPLAY_HOURS:
        errors.append(
            f"{hours} hours requested, exceeding the {MAX_REPLAY_HOURS}-hour cap"
        )

    return errors


def build_hourly_slots(start: datetime, end: datetime) -> list[datetime]:
    """
    範囲を毎時のスロットに分割する。EventBridge の実行単位と一致させる。

    分や秒は切り捨てる。スケジュールは毎時0分に発火するため、
    スロットがずれると元の実行と同じ fetched_at にならない。
    """
    cursor = start.replace(minute=0, second=0, microsecond=0)
    last = end.replace(minute=0, second=0, microsecond=0)

    slots = []
    while cursor <= last:
        slots.append(cursor)
        cursor += timedelta(hours=1)
    return slots


def build_replay_event(slot: datetime) -> dict:
    """
    スロットから EventBridge 互換のイベントを構築する。

    id を時刻から決定的に導出する理由:
        同じスロットを2回再生しても同じ id になる。ADR-005 の相関IDが
        そのまま機能し、ログ上で「同じ時間帯の試行」として束ねられる。
        ランダムな id にすると、再生のたびに別の実行に見える。
    """
    time_str = slot.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    event_id = "replay-" + hashlib.sha256(time_str.encode()).hexdigest()[:16]

    return {
        "id": event_id,
        "time": time_str,
        "source": "backfill.replay",
        "detail-type": "Scheduled Event",
        "detail": {},
    }


def exclude_completed(slots: list[datetime], completed: set[str]) -> list[datetime]:
    """
    完了済みのスロットを除く。

    冪等なので再実行しても壊れないが、それでもスキップする。
    コストと時間は冪等ではない。
    """
    return [
        s for s in slots
        if s.strftime("%Y-%m-%dT%H") not in completed
    ]


def build_batches(events: list[dict],
                  max_concurrent: int = MAX_CONCURRENT_REPLAYS) -> list[list[dict]]:
    """
    同時実行数を制限したバッチに分ける。

    72 実行を一斉に投げれば、毎時の本番スケジュールが同時実行枠を取れなくなる。
    急ぐ作業のために、動いている仕組みを壊さない。
    """
    return [
        events[i:i + max_concurrent]
        for i in range(0, len(events), max_concurrent)
    ]


def build_replay_plan(start: datetime, end: datetime, now: datetime,
                      completed: set[str] | None = None) -> dict:
    """
    再生計画をまとめて構築する。例外を投げず判断材料を返す。
    """
    errors = validate_window(start, end, now)
    if errors:
        return {"valid": False, "errors": errors, "events": [], "batches": []}

    slots = build_hourly_slots(start, end)
    remaining = exclude_completed(slots, completed or set())
    events = [build_replay_event(s) for s in remaining]

    return {
        "valid": True,
        "errors": [],
        "total_slots": len(slots),
        "skipped": len(slots) - len(remaining),
        "events": events,
        "batches": build_batches(events),
    }