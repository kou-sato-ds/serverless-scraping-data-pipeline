"""
Google News RSS Collector (Lambda Function) — v2.0 / Content-Addressable Storage

データパイプライン:
    1. 収集: Google News 公式 RSS フィード (XML) を取得
    2. 加工: タイトル・リンク・公開日時・発行元媒体を抽出/正規化
    3. 蓄積: 記事ごとに Content-Addressable な S3 キーで JSON 保存
             (year=YYYY/month=MM/day=DD/hour=HH/<sha256:16>.json)

設計上の特徴 (v2.0 で導入された冪等性保証):
    - **記事粒度の冪等性**: S3 キーは `link + published` の SHA-256 から決定的に
      生成される。同じ記事が複数回取得されても、S3 上では常に同一キーに
      書き込まれ、Athena クエリ結果が膨らまない (Content-Addressable Storage).
    - **パーティションは published_at ベース**: Lambda 実行時刻ではなく
      記事の公開時刻でパーティションを切る。Late-arriving data や時刻またぎの
      実行でも、データの論理的所属が一意に決まる。
    - **fetched_at は監査用メタデータとして payload に保持**: 物理的な取得時刻も
      失わない (audit trail).
    - **schema_version を payload に含める**: 将来のスキーマ進化に備える.

v1.0 からの破壊的変更:
    - S3 オブジェクトのスキーマが `{"count": N, "articles": [...]}` から
      `{"schema_version": "2.0", "article": {...}}` に変更 (1ファイル=1記事).
    - `build_object_key()` のシグネチャ変更: 第1引数が `datetime` から `dict` に.

設計判断の詳細は docs/ADR-002-content-addressable-keys.md を参照.
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

# ペイロードのスキーマバージョン。破壊的変更時にインクリメントすること。
SCHEMA_VERSION = "2.0"

# SHA-256 の先頭何文字を S3 キーに使うか。
# 16 文字 = 64bit の衝突空間 = 4 億記事で衝突確率 ~10^-6 (誕生日問題).
# 本パイプラインの想定スケール (月数千件) では衝突は実質的にゼロ.
HASH_PREFIX_LENGTH = 16


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
# 冪等性の核: 記事から決定的なハッシュとパーティションを導出
# ================================================================
def compute_article_hash(article: dict) -> str:
    """
    記事のユニーク識別子としての SHA-256 ハッシュを返す。

    ハッシュの源泉は `link` と `published`:
        - `link`: 記事の一意識別子 (Google News の場合 URL に CAAq... が含まれる)
        - `published`: 同じ URL が更新版として再配信されたケースを区別

    `published` が None の記事は link のみでハッシュ化する。
    これは「公開時刻不明な記事は同一 URL なら同一記事」として扱う方針.

    Returns:
        16 文字の 16 進数文字列 (例: "a3f5b9c8e1d2f4a6")
    """
    link = article.get("link", "")
    published = article.get("published") or ""
    digest_input = f"{link}|{published}".encode("utf-8")
    return hashlib.sha256(digest_input).hexdigest()[:HASH_PREFIX_LENGTH]


def derive_partition_datetime(article: dict, fallback: datetime) -> datetime:
    """
    パーティションキーに使う datetime を記事から導出する。

    優先順位:
        1. 記事の `published` (ISO 8601 文字列) があればそれを使用
        2. 無ければ fallback (通常は Lambda 実行時刻 = fetched_at) を使用

    fallback パスに入った記事は CloudWatch ログ + Custom Metric (将来 PR3 で
    実装) で観測すること。サイレントに誤ったパーティションに入らせない.
    """
    published = article.get("published")
    if published:
        try:
            # Python 3.11+ は "Z" もパース可。3.12 環境なので問題なし.
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            # tz-aware を保証 (theoretically 取得時は UTC のはずだが念のため)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            print(
                f"[WARN] failed to parse published={published!r}, "
                f"falling back to fetched_at"
            )
    return fallback


def build_object_key(article: dict, fetched_at: datetime) -> str:
    """
    Athena/Glue 互換の Hive パーティション + Content-Addressable ファイル名で
    S3 キーを生成する。

    形式:
        <prefix>/year=YYYY/month=MM/day=DD/hour=HH/<sha256:16>.json

    冪等性の保証:
        同じ `article` (link + published が同一) からは、何度呼んでも同じキーが
        返る。これにより S3 の PutObject は上書きセマンティクスとなり、
        リトライによるデータ重複が構造的に防がれる.

    Args:
        article: 記事 dict (link, published を含む)
        fetched_at: published が無い場合のパーティション fallback として使う UTC datetime
    """
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


# ================================================================
# 蓄積 (S3 Upload, 1 article = 1 object, Content-Addressable)
# ================================================================
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
    """
    全記事を 1 件ずつ S3 にアップロードする。

    Args:
        articles: fetch_articles() の返り値
        fetched_at: EventBridge イベント時刻 (Lambda 起動時刻ではない).
                    リトライ時も同じ値が渡るよう、event ペイロードから
                    決定される.

    Returns:
        書き込んだ S3 キーのリスト. SKIP_UPLOAD=true の場合は空リスト.

    Raises:
        RuntimeError: S3_BUCKET 未設定
        botocore.exceptions.ClientError: S3 PutObject の失敗 (boto3 標準の
            リトライを尽くした後の最終的な失敗のみ伝播する)
    """
    if SKIP_UPLOAD:
        print("[INFO] SKIP_UPLOAD=true — skipping S3 upload.")
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

        # NOTE: PutObject は冪等 (同一キーへの再書き込みは上書き). キーが
        # コンテンツから決定的に導出されるため、重複書き込みでも保存される
        # 内容は同じになる. これが Content-Addressable Storage の本質.
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=object_key,
            Body=body,
            ContentType="application/json",
        )
        written_keys.append(object_key)

    return written_keys


# ================================================================
# EventBridge イベントからの時刻抽出 (冪等性の基盤)
# ================================================================
def extract_event_time(event: dict) -> datetime:
    """
    EventBridge Scheduled Rule の `time` フィールドを UTC datetime として抽出する。

    EventBridge は同一イベントに対して同一の `time` を渡すため、リトライが
    発生してもこの値は変わらない. これが「fetched_at がリトライ間で安定する」
    という冪等性の保証になっている.

    event の形式 (EventBridge Scheduled Rule):
        {
          "version": "0",
          "id": "...",
          "detail-type": "Scheduled Event",
          "source": "aws.events",
          "time": "2026-05-11T03:00:00Z",
          ...
        }

    ローカル実行やテストで `time` が無い場合は datetime.now(utc) にフォール
    バックする (本番では絶対にこのパスに入らないこと).
    """
    time_str = event.get("time") if isinstance(event, dict) else None
    if time_str:
        try:
            return datetime.fromisoformat(time_str.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except (TypeError, ValueError):
            print(f"[WARN] failed to parse event['time']={time_str!r}")
    # フォールバック (ローカルテスト用). 本番ログでこれが出たら設計バグ.
    print("[WARN] event['time'] not found, falling back to wall clock")
    return datetime.now(timezone.utc)


# ================================================================
# Lambda Handler
# ================================================================
def lambda_handler(event, context):
    """
    EventBridge から1時間ごとに呼ばれるエントリポイント。

    冪等性の動作:
        - 同じ EventBridge イベントが再配信されても、event['time'] は不変
        - 各記事の S3 キーは link + published から決定的に生成される
        - したがって、何度呼び出されても S3 の最終状態は同一に収束する

    注意:
        本 handler は v2.0 時点では例外を握りつぶさず、そのまま伝播させる
        設計に未変更 (PR1 のスコープ外). PR2 で Lambda の retry / DLQ 機構を
        有効化するため、structured exception handling を導入予定.
    """
    fetched_at = extract_event_time(event)
    print(
        f"[INFO] RSS fetch start: feed={RSS_URL}, max={MAX_ARTICLES}, "
        f"fetched_at={fetched_at.isoformat()}"
    )
    try:
        articles = fetch_articles()
        print(f"[INFO] Parsed {len(articles)} articles.")

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
            # 全キーを返すと payload が膨らむので最初の1件のみサンプル提示
            result["sample_key"] = written_keys[0]

        return {
            "statusCode": 200,
            "body": json.dumps(result, ensure_ascii=False),
        }

    except Exception as e:
        # NOTE: 現状は v1.0 と同じく握りつぶし継続. PR2 で raise に変更予定.
        # この時点で改修すると Lambda retry が発動してデータ重複になる
        # リスクがあるため、PR1 では冪等性導入に専念する.
        print(f"[ERROR] {type(e).__name__}: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False
            ),
        }