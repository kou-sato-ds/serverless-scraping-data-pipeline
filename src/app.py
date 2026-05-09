"""
Google News RSS Collector (Lambda Function)

データパイプライン:
    1. 収集: Google News 公式 RSS フィード (XML) を取得
    2. 加工: タイトル・リンク・公開日時・発行元媒体を抽出/正規化
    3. 蓄積: Hive 互換パーティションで S3 に JSON 保存
             (year=YYYY/month=MM/day=DD/hour=HH/HHMMSS.json)

設計上の特徴:
    - feedparser のみでスクレイピング不要 (Chromium 不使用)
    - ZIP デプロイ (Container Image 不要、デプロイ時間 1/10 以下)
    - Athena/Glue で直接クエリ可能なパーティション設計
    - SKIP_UPLOAD によるローカルテスト切替
    - get_s3_client() の遅延初期化でテスタビリティとコールドスタート両立
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Optional

import boto3
import feedparser

# ----------------------------------------------------------------
# 環境変数 (template.yaml で注入)
# ----------------------------------------------------------------
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_PREFIX = os.environ.get("S3_PREFIX", "google-news-rss")
RSS_URL = os.environ.get(
    "RSS_URL",
    "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja",
)
MAX_ARTICLES = int(os.environ.get("MAX_ARTICLES", "100"))
SKIP_UPLOAD = os.environ.get("SKIP_UPLOAD", "false").lower() == "true"


# ----------------------------------------------------------------
# AWS クライアントの遅延初期化
#   - 本番: 初回呼び出しでキャッシュされ、コンテナ再利用時は即時返却
#   - テスト: cache_clear() でテスト間の状態を分離可能
# ----------------------------------------------------------------
@lru_cache(maxsize=1)
def get_s3_client():
    return boto3.client("s3")


# ================================================================
# データクレンジング
# ================================================================
def cleanse_text(text: Optional[str]) -> str:
    """空白・改行・全角スペース・制御文字を正規化する。"""
    if not text:
        return ""
    cleaned = re.sub(r"[\s\u3000]+", " ", text)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cleaned)
    return cleaned.strip()


def parse_published(entry) -> Optional[str]:
    """
    feedparser の published_parsed (UTC time.struct_time) を ISO 8601 文字列に変換。
    フィードに pubDate が無い場合は None を返す。
    """
    parsed = getattr(entry, "published_parsed", None)
    if not parsed:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError):
        return None


def extract_source(entry) -> Optional[str]:
    """
    RSS の <source> 要素から発行元媒体名を取得する (例: '日本経済新聞')。
    """
    source = getattr(entry, "source", None)
    if source is None:
        return None
    title = getattr(source, "title", None) or (
        source.get("title") if isinstance(source, dict) else None
    )
    return cleanse_text(title) if title else None


# ================================================================
# 収集 (RSS Fetch)
# ================================================================
def fetch_articles() -> list[dict]:
    """Google News RSS をパースして記事リストを返す。"""
    feed = feedparser.parse(RSS_URL)

    if feed.bozo:
        # bozo=1 はパース時の警告。多くの場合 entries は使える。
        print(f"[WARN] feedparser bozo: {feed.bozo_exception}")

    articles: list[dict] = []
    seen_links: set[str] = set()

    for entry in feed.entries[:MAX_ARTICLES]:
        title = cleanse_text(getattr(entry, "title", ""))
        link = (getattr(entry, "link", "") or "").strip()

        if not title or not link or link in seen_links:
            continue
        seen_links.add(link)

        articles.append(
            {
                "title": title,
                "link": link,
                "published": parse_published(entry),
                "source": extract_source(entry),
            }
        )

    return articles


# ================================================================
# 蓄積 (S3 Upload, Hive-compatible partitioning)
# ================================================================
def build_object_key(now: datetime) -> str:
    """Athena/Glue 互換の Hive パーティション形式でキーを生成。"""
    return (
        f"{S3_PREFIX}"
        f"/year={now.year:04d}"
        f"/month={now.month:02d}"
        f"/day={now.day:02d}"
        f"/hour={now.hour:02d}"
        f"/{now.strftime('%H%M%S')}.json"
    )


def upload_to_s3(articles: list[dict]) -> Optional[str]:
    """JSON ペイロードを S3 にアップロード。"""
    if SKIP_UPLOAD:
        print("[INFO] SKIP_UPLOAD=true — skipping S3 upload.")
        return None
    if not S3_BUCKET:
        raise RuntimeError("Environment variable S3_BUCKET is not set.")

    now = datetime.now(timezone.utc)
    object_key = build_object_key(now)

    payload = {
        "fetched_at": now.isoformat(),
        "source_feed": RSS_URL,
        "count": len(articles),
        "articles": articles,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    get_s3_client().put_object(
        Bucket=S3_BUCKET,
        Key=object_key,
        Body=body,
        ContentType="application/json",
    )
    return object_key


# ================================================================
# Lambda Handler
# ================================================================
def lambda_handler(event, context):
    """EventBridge から1時間ごとに呼ばれる。"""
    print(f"[INFO] RSS fetch start: feed={RSS_URL}, max={MAX_ARTICLES}")
    try:
        articles = fetch_articles()
        print(f"[INFO] Parsed {len(articles)} articles.")

        if not articles:
            return {
                "statusCode": 204,
                "body": json.dumps(
                    {"message": "No articles parsed."}, ensure_ascii=False
                ),
            }

        object_key = upload_to_s3(articles)

        result = {
            "message": "Successfully fetched RSS.",
            "count": len(articles),
        }
        if object_key:
            result["bucket"] = S3_BUCKET
            result["key"] = object_key

        return {
            "statusCode": 200,
            "body": json.dumps(result, ensure_ascii=False),
        }

    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False
            ),
        }
