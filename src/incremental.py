"""
増分取得 — 前回どこまで取ったかを S3 に記録する。

ADR-014: ウォーターマークを S3 に保存し、成功後にだけ前進させる。

背景:
    ADR-002 の冪等キーにより、同じ記事を何度書いても S3 の最終状態は変わらない。
    しかし「壊れない」ことと「無駄がない」ことは別である。
    毎時フィード全件を PUT し直せば、リクエスト課金は記事数に比例して払い続ける。

    Lambda は状態を持てないため、ウォーターマークは S3 のオブジェクトとして保存する。
    姉妹プロジェクトの item 106 と同じ3原則を適用する:
      - >= と lookback で多めに読む (遅延到着・同時刻の取りこぼし防止)
      - アップロード成功後にだけ前進させる (失敗区間の永久欠落防止)
      - 後退させない
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from botocore.exceptions import ClientError

STATE_KEY = "state/incremental_watermark.json"

# RSS の published は配信側の都合で前後する。広めに取り、重複は ADR-002 のキーで吸収する。
LOOKBACK = timedelta(hours=2)


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    """ISO 8601 を UTC の datetime に変換する。解釈できなければ None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def select_new_articles(articles: list[dict], watermark: Optional[datetime],
                        lookback: timedelta = LOOKBACK) -> list[dict]:
    """
    ウォーターマーク - lookback 以降の記事を返す。

    published が無い記事は **含める**。判定できないものを捨てれば取りこぼしになるが、
    含めて重複しても ADR-002 のキーで同じ場所に収束するだけである。
    """
    if watermark is None:
        return list(articles)

    start = watermark - lookback
    selected = []
    for a in articles:
        published = parse_ts(a.get("published"))
        if published is None or published >= start:
            selected.append(a)
    return selected


def next_watermark(articles: list[dict], current: Optional[datetime]) -> Optional[datetime]:
    """次のウォーターマークを返す。決して後退させない。"""
    times = [t for t in (parse_ts(a.get("published")) for a in articles) if t]
    if not times:
        return current
    newest = max(times)
    return newest if current is None else max(newest, current)


def load_watermark(s3, bucket: str, key: str = STATE_KEY) -> Optional[datetime]:
    """
    S3 からウォーターマークを読む。初回（オブジェクトが無い）は None。

    NoSuchKey 以外のエラーは握りつぶさない。権限エラーを「初回」と誤認すれば、
    毎回全件を取り直す状態が静かに続く（ADR-003 と同じ思想）。
    """
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise
    body = json.loads(obj["Body"].read())
    return parse_ts(body.get("watermark"))


def save_watermark(s3, bucket: str, value: Optional[datetime],
                   key: str = STATE_KEY) -> None:
    """ウォーターマークを S3 に保存する。"""
    if value is None:
        return
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps({"watermark": value.isoformat()}).encode("utf-8"),
        ContentType="application/json",
    )


def run_increment(s3, bucket: str, articles: list[dict],
                  upload_fn: Callable[[list[dict]], None]) -> dict:
    """
    1回分の増分取得を実行する。

    ウォーターマークはアップロード成功後にだけ保存する。
    upload_fn が例外を投げれば保存は行われず、次回同じ区間を読み直す。
    """
    current = load_watermark(s3, bucket)
    batch = select_new_articles(articles, current)
    upload_fn(batch)
    new = next_watermark(batch, current)
    save_watermark(s3, bucket, new)
    return {"selected": len(batch), "watermark": new}