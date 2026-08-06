"""
Google News RSS Collector (Lambda Function) — v3.0 / Powertools Structured Logging

設計上の特徴:
    - **記事粒度の冪等性 (v2.0 / ADR-002)**: S3 キーは `link + published` の
      SHA-256 から決定的に生成される。
    - **例外の伝播 (v2.1 / ADR-003)**: handler は例外を握りつぶさず再送出し、
      Lambda の自動リトライと SQS DLQ を発動させる。
    - **構造化ログ (v3.0 / ADR-004)**: `print(json.dumps(...))` による手書きの
      構造化を廃し、Lambda Powertools Logger へ移行。ADR-003 が
      「PR#3 で置換」と刻んだ暫定実装の解消にあたる。

v3.0 (PR #3) での変更:
    - aws-lambda-powertools を依存に追加
    - print(json.dumps(...)) / print(f"[INFO] ...") を Logger へ置換
    - NOTE: @logger.inject_lambda_context は本PRのスコープ外 (後続PRで対応)。
      デコレータは context オブジェクトを要求するが、現行テストは None を渡すため。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import boto3
import feedparser
from aws_lambda_powertools import Logger

# ----------------------------------------------------------------
# 構造化ロガー (ADR-004)
# ----------------------------------------------------------------
logger = Logger(service="rss-collector")

S3_BUCKET = os.environ.get("S3_BUCKET")
S3_PREFIX = os.environ.get("S3_PREFIX", "google-news-rss")
RSS_URL = os.environ.get(
    "RSS_URL",
    "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja",
)
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "100"))
SKIP_UPLOAD = os.environ.get("SKIP_UPLOAD", "false").lower() == "true"

SCHEMA_VERSION = "2.0"
HASH_PREFIX_LENGTH = 16


@lru_cache(maxsize=1)
def get_s3_client():
    return boto3.client("s3")


def cleanse_text(text: Optional[str]) -> str:
    """空白・改行・全角スペース・制御文字を正規化する。"""
    if not text:
        return ""
    cleaned = re.sub(r"[\s\u3000]+", " ", text)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cleaned)
    return cleaned.strip()


def parse_published(entry) -> Optional[str]:
    """published_parsed (UTC time.struct_time) を ISO 8601 文字列に変換。"""
    parsed = getattr(entry, "published_parsed", None)
    if not parsed:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError):
        return None


def extract_source(entry) -> Optional[str]:
    """RSS の <source> 要素から発行元媒体名を取得する。"""
    source = getattr(entry, "source", None)
    if source is None:
        return None
    title = getattr(source, "title", None) or (
        source.get("title") if isinstance(source, dict) else None
    )
    return cleanse_text(title) if title else None


def fetch_articles() -> list[dict]:
    """Google News RSS をパースして記事リストを返す。"""
    feed = feedparser.parse(RSS_URL)

    if feed.bozo:
        logger.warning("feedparser reported a parse warning",
                       extra={"bozo_exception": str(feed.bozo_exception)})

    articles: list[dict] = []
    seen_links: set[str] = set()

    for entry in feed.entries[:MAX_ARTICLES]:
        title = cleanse_text(getattr(entry, "title", ""))
        link = (getattr(entry, "link", "") or "").strip()

        if not title or not link or link in seen_links:
            continue
        seen_links.add(link)

        articles.append({
            "title": title,
            "link": link,
            "published": parse_published(entry),
            "source": extract_source(entry),
        })

    return articles


def compute_article_hash(article: dict) -> str:
    """記事のユニーク識別子としての SHA-256 ハッシュ(先頭16文字)を返す。"""
    link = article.get("link", "")
    published = article.get("published") or ""
    digest_input = f"{link}|{published}".encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()[:HASH_PREFIX_LENGTH]


def derive_partition_datetime(article: dict, fallback: datetime) -> datetime:
    """パーティションキーに使う datetime を記事から導出する。"""
    published = article.get("published")
    if published:
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            logger.warning("failed to parse published timestamp; using fetched_at",
                           extra={"published": published})
    return fallback


def build_object_key(article: dict, fetched_at: datetime) -> str:
    """Hive パーティション + Content-Addressable ファイル名で S3 キーを生成する。"""
    partition_dt = derive_partition_datetime(article, fetched_at)
    article_hash = compute_article_hash(article)
    return (
        f"{S3_PREFIX}"
        f"/year={partition_dt.year:04d}"
        f"/month={partition_dt.month:02d}"
        f"/day={partition_dt.day:02d}"
        f"/hour={partition_dt.hour:02d}"
        f"/{article_hash}.json"
    )


def build_payload(article: dict, fetched_at: datetime, article_hash: str) -> dict:
    """S3 に書き込む 1 記事ぶんの JSON ペイロードを構築する。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "fetched_at": fetched_at.isoformat(),
        "source_feed": RSS_URL,
        "article_hash": article_hash,
        "article": article,
    }


def upload_articles(articles: list[dict], fetched_at: datetime) -> list[str]:
    """全記事を 1 件ずつ S3 にアップロードする。"""
    if SKIP_UPLOAD:
        logger.info("SKIP_UPLOAD is enabled; skipping S3 upload")
        return []
    if not S3_BUCKET:
        raise RuntimeError("Environment variable S3_BUCKET is not set.")

    s3 = get_s3_client()
    written_keys: list[str] = []

    for article in articles:
        article_hash = compute_article_hash(article)
        object_key = build_object_key(article, fetched_at)
        payload = build_payload(article, fetched_at, article_hash)
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

        # NOTE: PutObject は冪等。キーがコンテンツから決定的に導出されるため、
        # 重複書き込みでも保存内容は同じになる (ADR-002)。
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=object_key,
            Body=body,
            ContentType="application/json",
        )
        written_keys.append(object_key)

    return written_keys


def extract_event_time(event: dict) -> datetime:
    """EventBridge Scheduled Rule の `time` を UTC datetime として抽出する。"""
    time_str = event.get("time") if isinstance(event, dict) else None
    if time_str:
        try:
            return datetime.fromisoformat(time_str.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except (TypeError, ValueError):
            logger.warning("failed to parse event time", extra={"event_time": time_str})
    logger.warning("event['time'] not found; falling back to wall clock")
    return datetime.now(timezone.utc)


def lambda_handler(event, context):
    """
    EventBridge から1時間ごとに呼ばれるエントリポイント。

    冪等性 (ADR-002): 同じイベントが再配信されても S3 の最終状態は同一に収束する。
    エラー伝播 (ADR-003): 例外は構造化 ERROR ログの後、そのまま再送出する。
    構造化ログ (ADR-004): 書式は Powertools Logger が保証し、手書きしない。
    """
    fetched_at = extract_event_time(event)
    logger.info("RSS fetch start", extra={
        "feed": RSS_URL,
        "max_articles": MAX_ARTICLES,
        "fetched_at": fetched_at.isoformat(),
    })

    try:
        articles = fetch_articles()
        logger.info("articles parsed", extra={"count": len(articles)})

        if not articles:
            return {
                "statusCode": 204,
                "body": json.dumps(
                    {"message": "No articles parsed.", "fetched_at": fetched_at.isoformat()},
                    ensure_ascii=False,
                ),
            }

        written_keys = upload_articles(articles, fetched_at)

        result = {
            "message": "Successfully fetched RSS.",
            "count": len(articles),
            "fetched_at": fetched_at.isoformat(),
        }
        if written_keys:
            result["bucket"] = S3_BUCKET
            result["written_count"] = len(written_keys)
            result["sample_key"] = written_keys[0]

        return {"statusCode": 200, "body": json.dumps(result, ensure_ascii=False)}

    except Exception as e:
        # ADR-004: 書式を手書きせず Logger に委ねる。
        # logger.exception() は severity=ERROR とスタックトレースを自動付与する。
        logger.exception("RSS pipeline failed", extra={
            "error_type": type(e).__name__,
            "error_message": str(e),
            "event_id": event.get("id") if isinstance(event, dict) else None,
            "fetched_at": fetched_at.isoformat(),
        })
        raise