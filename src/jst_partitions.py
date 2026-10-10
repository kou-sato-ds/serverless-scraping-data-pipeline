"""
日本時間の1日は、UTC パーティション2つにまたがる。

ADR-015: 保存は UTC のまま、読むときに日本時間の1日を正しく引く。

背景:
    S3 のパーティションは UTC の日付で切られている。
    日本時間 10/10 の1日は UTC では 10/9 15:00 〜 10/10 15:00 であり、
    dt=2026-10-09 と dt=2026-10-10 の2つにまたがる。

    「10/10 の記事」を dt=2026-10-10 だけで探せば、
    日本時間 0:00〜8:59 の記事が丸ごと漏れる。エラーは出ない。

    保存側を日本時間に変える選択肢もあるが、既存データとの整合が崩れる。
    保存は UTC のまま固定し、読む側で「どのパーティションを、どの範囲で読むか」を
    計算する方が、過去データを書き換えずに済む。

    日本には夏時間が無いため、固定オフセット(+9:00)で正確に扱える。
    姉妹プロジェクトの item 119 を AWS 側へ移植したもの。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

JST = timezone(timedelta(hours=9), name="JST")
PARTITION_FORMAT = "dt={:%Y-%m-%d}"


def jst_day_of(ts: datetime) -> date:
    """時刻が属する日本時間の日付。タイムゾーンの無い時刻は拒否する。"""
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"naive datetime is not allowed: {ts.isoformat()}")
    return ts.astimezone(JST).date()


def utc_bounds_for_jst_day(d: date) -> tuple[datetime, datetime]:
    """日本時間 d の1日を、UTC の半開区間 [start, end) で返す。"""
    start = datetime.combine(d, time.min, tzinfo=JST)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def utc_partitions_for_jst_day(d: date) -> list[str]:
    """
    日本時間 d の1日を読むために必要な UTC パーティション。

    必ず2つになる(+9時間ずれているため)。1つしか読まなければ、9時間分が漏れる。
    """
    start, end = utc_bounds_for_jst_day(d)
    last = end - timedelta(microseconds=1)
    return sorted({PARTITION_FORMAT.format(start), PARTITION_FORMAT.format(last)})


def read_plan(d: date) -> dict[str, object]:
    """
    読み方の一式を返す。

    パーティションで読む範囲を絞り(コスト)、時刻で正確に切る(正しさ)。
    パーティションだけでは前後に15時間ずつ余計なデータが混ざる。
    """
    start, end = utc_bounds_for_jst_day(d)
    return {
        "partitions": utc_partitions_for_jst_day(d),
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
    }