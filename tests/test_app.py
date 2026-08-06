"""
Unit and integration tests for the RSS collector Lambda (v2.1).

v2.0 で追加されたテスト群:
    - TestComputeArticleHash: 記事ハッシュの決定性・衝突性
    - TestDerivePartitionDatetime: パーティション時刻の導出ロジック
    - TestExtractEventTime: EventBridge イベントからの時刻抽出
    - TestIdempotency: 同一入力での再実行が S3 上で重複を生まないこと (冪等性の本丸)

v2.1 (PR #2) での変更:
    - TestLambdaHandler: 例外が握りつぶされず伝播することを検証する形へ変更
      (test_raises_on_exception). Lambda retry / DLQ 発動の前提条件.
    - 構造化 ERROR ログの契約テストを追加 (test_error_log_is_structured_json).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

import app


# ================================================================
# Fixtures
# ================================================================
@pytest.fixture
def sample_feed_xml() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "sample_feed.xml"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def s3_environment():
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-northeast-1")
        s3.create_bucket(
            Bucket="test-bucket",
            CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"},
        )
        app.get_s3_client.cache_clear()
        yield s3
        app.get_s3_client.cache_clear()


@pytest.fixture
def patched_feedparser(monkeypatch, sample_feed_xml):
    import feedparser
    original_parse = feedparser.parse
    monkeypatch.setattr(
        "app.feedparser.parse", lambda url: original_parse(sample_feed_xml)
    )
    return original_parse


@pytest.fixture
def fixed_event():
    """EventBridge Scheduled Rule の典型的なペイロード (リトライ時も同一)。"""
    return {
        "version": "0",
        "id": "cdc73f9d-aea9-11e3-9d5a-835b769c0d9c",
        "detail-type": "Scheduled Event",
        "source": "aws.events",
        "account": "123456789012",
        "time": "2026-05-11T03:00:00Z",
        "region": "ap-northeast-1",
        "resources": ["arn:aws:events:ap-northeast-1:123456789012:rule/test"],
        "detail": {},
    }


# ================================================================
# cleanse_text
# ================================================================
class TestCleanseText:
    def test_normalizes_whitespace(self):
        assert app.cleanse_text("hello   world") == "hello world"

    def test_normalizes_newlines_and_tabs(self):
        assert app.cleanse_text("hello\n\tworld") == "hello world"

    def test_normalizes_full_width_space(self):
        assert app.cleanse_text("日本　ニュース") == "日本 ニュース"

    def test_strips_control_chars(self):
        assert app.cleanse_text("hello\x00world\x07") == "helloworld"

    def test_strips_outer_whitespace(self):
        assert app.cleanse_text("  hello  ") == "hello"

    @pytest.mark.parametrize("falsy", ["", None])
    def test_returns_empty_for_falsy(self, falsy):
        assert app.cleanse_text(falsy) == ""


# ================================================================
# parse_published
# ================================================================
class TestParsePublished:
    def test_with_valid_struct_time(self):
        class Entry:
            published_parsed = (2026, 5, 9, 12, 30, 0, 4, 129, 0)
        assert app.parse_published(Entry()) == "2026-05-09T12:30:00+00:00"

    def test_returns_none_when_missing(self):
        class Entry:
            pass
        assert app.parse_published(Entry()) is None

    def test_returns_none_for_invalid_value(self):
        class Entry:
            published_parsed = "not-a-tuple"
        assert app.parse_published(Entry()) is None


# ================================================================
# extract_source
# ================================================================
class TestExtractSource:
    def test_extracts_source_title(self):
        class Source:
            title = "日本経済新聞"
        class Entry:
            source = Source()
        assert app.extract_source(Entry()) == "日本経済新聞"

    def test_returns_none_when_missing(self):
        class Entry:
            pass
        assert app.extract_source(Entry()) is None


# ================================================================
# compute_article_hash (v2.0 新規)
# ================================================================
class TestComputeArticleHash:
    def test_deterministic_same_input_same_hash(self):
        """同じ記事を何回ハッシュ化しても同じ値が返ること。"""
        article = {
            "title": "テスト記事",
            "link": "https://example.com/article/1",
            "published": "2026-05-09T12:00:00+00:00",
            "source": "テスト媒体",
        }
        h1 = app.compute_article_hash(article)
        h2 = app.compute_article_hash(article)
        h3 = app.compute_article_hash(dict(article))  # 別オブジェクトでも同じ
        assert h1 == h2 == h3

    def test_different_links_different_hashes(self):
        a = {"link": "https://example.com/a", "published": "2026-05-09T12:00:00+00:00"}
        b = {"link": "https://example.com/b", "published": "2026-05-09T12:00:00+00:00"}
        assert app.compute_article_hash(a) != app.compute_article_hash(b)

    def test_different_published_different_hashes(self):
        """同じ URL でも公開時刻が異なれば別記事として扱う (更新版検知)。"""
        a = {"link": "https://example.com/x", "published": "2026-05-09T12:00:00+00:00"}
        b = {"link": "https://example.com/x", "published": "2026-05-10T12:00:00+00:00"}
        assert app.compute_article_hash(a) != app.compute_article_hash(b)

    def test_title_does_not_affect_hash(self):
        """タイトルが微修正されても (link+published 同じなら) 同一記事として扱う。"""
        a = {
            "title": "元のタイトル",
            "link": "https://example.com/y",
            "published": "2026-05-09T12:00:00+00:00",
        }
        b = {
            "title": "修正されたタイトル",
            "link": "https://example.com/y",
            "published": "2026-05-09T12:00:00+00:00",
        }
        assert app.compute_article_hash(a) == app.compute_article_hash(b)

    def test_hash_length_is_configured(self):
        article = {"link": "https://example.com/z", "published": None}
        h = app.compute_article_hash(article)
        assert len(h) == app.HASH_PREFIX_LENGTH
        # 16進数文字列であること
        int(h, 16)  # 例外が出なければ OK

    def test_handles_none_published(self):
        a = {"link": "https://example.com/p", "published": None}
        b = {"link": "https://example.com/p", "published": None}
        assert app.compute_article_hash(a) == app.compute_article_hash(b)


# ================================================================
# derive_partition_datetime (v2.0 新規)
# ================================================================
class TestDerivePartitionDatetime:
    def test_uses_published_when_present(self):
        article = {"published": "2026-05-09T15:30:00+00:00"}
        fallback = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = app.derive_partition_datetime(article, fallback)
        assert result.year == 2026
        assert result.month == 5
        assert result.day == 9
        assert result.hour == 15

    def test_falls_back_when_published_none(self):
        article = {"published": None}
        fallback = datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)
        result = app.derive_partition_datetime(article, fallback)
        assert result == fallback

    def test_falls_back_on_invalid_published(self):
        article = {"published": "not-a-valid-iso-string"}
        fallback = datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)
        result = app.derive_partition_datetime(article, fallback)
        assert result == fallback

    def test_handles_z_suffix(self):
        article = {"published": "2026-05-09T15:30:00Z"}
        fallback = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = app.derive_partition_datetime(article, fallback)
        assert result.hour == 15


# ================================================================
# build_object_key (v2.0 改修)
# ================================================================
class TestBuildObjectKey:
    def test_uses_published_for_partition(self):
        """パーティションは記事の published_at から決まること。"""
        article = {
            "link": "https://example.com/a",
            "published": "2026-05-09T13:19:50+00:00",
        }
        # fetched_at は別日 — published 優先で 5/9 パーティションになるべき
        fetched_at = datetime(2099, 12, 31, 23, 0, tzinfo=timezone.utc)
        key = app.build_object_key(article, fetched_at)
        assert "year=2026" in key
        assert "month=05" in key
        assert "day=09" in key
        assert "hour=13" in key

    def test_uses_fetched_at_when_published_missing(self):
        article = {"link": "https://example.com/a", "published": None}
        fetched_at = datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)
        key = app.build_object_key(article, fetched_at)
        assert "year=2026" in key
        assert "month=05" in key
        assert "day=11" in key
        assert "hour=03" in key

    def test_filename_is_article_hash(self):
        article = {
            "link": "https://example.com/a",
            "published": "2026-05-09T13:00:00+00:00",
        }
        fetched_at = datetime(2026, 5, 9, 13, 0, tzinfo=timezone.utc)
        key = app.build_object_key(article, fetched_at)
        expected_hash = app.compute_article_hash(article)
        assert key.endswith(f"{expected_hash}.json")

    def test_zero_padding(self):
        article = {
            "link": "https://example.com/a",
            "published": "2026-01-02T03:04:05+00:00",
        }
        fetched_at = datetime(2026, 1, 2, 3, tzinfo=timezone.utc)
        key = app.build_object_key(article, fetched_at)
        assert "year=2026" in key
        assert "month=01" in key
        assert "day=02" in key
        assert "hour=03" in key


# ================================================================
# extract_event_time (v2.0 新規)
# ================================================================
class TestExtractEventTime:
    def test_parses_eventbridge_time(self, fixed_event):
        result = app.extract_event_time(fixed_event)
        assert result == datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)

    def test_falls_back_when_no_time(self):
        # time フィールドが無いイベントでもクラッシュせず datetime を返すこと
        result = app.extract_event_time({})
        assert isinstance(result, datetime)
        assert result.tzinfo is not None

    def test_falls_back_on_malformed_time(self):
        result = app.extract_event_time({"time": "not-iso"})
        assert isinstance(result, datetime)


# ================================================================
# fetch_articles
# ================================================================
class TestFetchArticles:
    def test_extracts_articles_from_sample_feed(self, patched_feedparser):
        articles = app.fetch_articles()
        assert len(articles) == 3
        first = articles[0]
        assert first["title"] == "日経平均、3万円台を回復 半導体株が牽引"
        assert first["link"].startswith("https://news.google.com/rss/articles/")
        assert first["published"] == "2026-05-08T11:30:00+00:00"
        assert first["source"] == "日本経済新聞"

    def test_cleansing_applied(self, patched_feedparser):
        articles = app.fetch_articles()
        third_title = articles[2]["title"]
        assert "  " not in third_title
        assert "\n" not in third_title
        assert third_title.startswith("空白だらけ")

    def test_deduplication(self, patched_feedparser):
        articles = app.fetch_articles()
        links = [a["link"] for a in articles]
        assert len(links) == len(set(links))

    def test_max_articles_limit(self, patched_feedparser, monkeypatch):
        monkeypatch.setattr("app.MAX_ARTICLES", 1)
        articles = app.fetch_articles()
        assert len(articles) == 1


# ================================================================
# upload_articles (v2.0 改修: 旧 upload_to_s3 から名前変更, 複数オブジェクト書き出し)
# ================================================================
class TestUploadArticles:
    def test_writes_one_object_per_article(self, s3_environment, monkeypatch):
        monkeypatch.setattr("app.SKIP_UPLOAD", False)
        fetched_at = datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)
        articles = [
            {
                "title": "記事A",
                "link": "https://example.com/a",
                "published": "2026-05-09T12:00:00+00:00",
                "source": "媒体A",
            },
            {
                "title": "記事B",
                "link": "https://example.com/b",
                "published": "2026-05-09T13:00:00+00:00",
                "source": "媒体B",
            },
        ]
        keys = app.upload_articles(articles, fetched_at)
        assert len(keys) == 2
        assert len(set(keys)) == 2  # 異なるキー

        for key in keys:
            obj = s3_environment.get_object(Bucket="test-bucket", Key=key)
            body = json.loads(obj["Body"].read().decode("utf-8"))
            assert body["schema_version"] == "2.0"
            assert "article" in body
            assert "article_hash" in body
            assert body["fetched_at"] == fetched_at.isoformat()
            assert obj["ContentType"] == "application/json"

    def test_skips_upload_when_flag_set(self, s3_environment, monkeypatch):
        monkeypatch.setattr("app.SKIP_UPLOAD", True)
        fetched_at = datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)
        result = app.upload_articles(
            [{"title": "X", "link": "Y", "published": None, "source": None}],
            fetched_at,
        )
        assert result == []

    def test_raises_when_bucket_not_set(self, s3_environment, monkeypatch):
        monkeypatch.setattr("app.SKIP_UPLOAD", False)
        monkeypatch.setattr("app.S3_BUCKET", None)
        fetched_at = datetime(2026, 5, 11, 3, 0, tzinfo=timezone.utc)
        with pytest.raises(RuntimeError, match="S3_BUCKET"):
            app.upload_articles(
                [{"title": "X", "link": "Y", "published": None, "source": None}],
                fetched_at,
            )


# ================================================================
# Idempotency (v2.0 の中核) — PR1 で達成した品質の証明
# ================================================================
class TestIdempotency:
    """
    冪等性の証明: 同一の EventBridge イベントを複数回処理しても、
    S3 上のオブジェクト数・内容が同一に収束することを保証する。
    """

    def test_same_event_produces_same_keys(
        self, s3_environment, patched_feedparser, fixed_event, monkeypatch
    ):
        """同じイベントを2回実行 → S3 オブジェクト数が変わらないこと。"""
        monkeypatch.setattr("app.SKIP_UPLOAD", False)

        # 1回目の実行
        response_1 = app.lambda_handler(fixed_event, None)
        assert response_1["statusCode"] == 200

        objects_after_first = s3_environment.list_objects_v2(Bucket="test-bucket")
        keys_after_first = sorted(
            obj["Key"] for obj in objects_after_first.get("Contents", [])
        )
        first_count = len(keys_after_first)
        assert first_count > 0  # 何らかが書かれた

        # 2回目の実行 (リトライ相当)
        response_2 = app.lambda_handler(fixed_event, None)
        assert response_2["statusCode"] == 200

        objects_after_second = s3_environment.list_objects_v2(Bucket="test-bucket")
        keys_after_second = sorted(
            obj["Key"] for obj in objects_after_second.get("Contents", [])
        )

        # オブジェクト数が変わっていないこと = 同じキーに上書きされた = 冪等
        assert keys_after_second == keys_after_first, (
            f"Idempotency violated: 1st run wrote {first_count} keys, "
            f"2nd run resulted in {len(keys_after_second)} keys. "
            f"Diff: {set(keys_after_second) - set(keys_after_first)}"
        )

    def test_different_event_times_same_partition_when_published_present(
        self, s3_environment, patched_feedparser, monkeypatch
    ):
        """
        EventBridge の time が変わっても、記事の published_at が同じなら
        S3 キーは同じになる (published_at がパーティションの SSOT である証明).
        """
        monkeypatch.setattr("app.SKIP_UPLOAD", False)

        event_a = {"time": "2026-05-10T03:00:00Z"}
        event_b = {"time": "2026-05-11T03:00:00Z"}

        app.lambda_handler(event_a, None)
        keys_a = sorted(
            o["Key"]
            for o in s3_environment.list_objects_v2(Bucket="test-bucket").get(
                "Contents", []
            )
        )

        # バケットをクリアしてから2回目
        for k in keys_a:
            s3_environment.delete_object(Bucket="test-bucket", Key=k)

        app.lambda_handler(event_b, None)
        keys_b = sorted(
            o["Key"]
            for o in s3_environment.list_objects_v2(Bucket="test-bucket").get(
                "Contents", []
            )
        )

        # sample_feed.xml の全記事に published があるので、キーは完全一致するはず
        assert keys_a == keys_b, (
            "Partition depends on event['time'] instead of article['published']. "
            "This breaks late-arriving data handling."
        )


# ================================================================
# lambda_handler (v2.1 改修: 例外伝播 + 構造化 ERROR ログの契約)
# ================================================================
class TestLambdaHandler:
    def test_full_flow_returns_200(
        self, s3_environment, patched_feedparser, fixed_event, monkeypatch
    ):
        monkeypatch.setattr("app.SKIP_UPLOAD", False)

        response = app.lambda_handler(fixed_event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["count"] == 3
        assert body["written_count"] == 3
        assert body["bucket"] == "test-bucket"
        assert "sample_key" in body
        assert body["fetched_at"] == "2026-05-11T03:00:00+00:00"

    def test_returns_204_when_no_articles(self, s3_environment, fixed_event, monkeypatch):
        class EmptyFeed:
            entries = []
            bozo = 0
        monkeypatch.setattr("app.feedparser.parse", lambda url: EmptyFeed())

        response = app.lambda_handler(fixed_event, None)
        assert response["statusCode"] == 204

    def test_raises_on_exception(self, s3_environment, fixed_event, monkeypatch):
        """ADR-003: 例外は握りつぶされず伝播すること (retry/DLQ の前提)。"""
        def boom(url):
            raise RuntimeError("Network down")
        monkeypatch.setattr("app.feedparser.parse", boom)

        with pytest.raises(RuntimeError, match="Network down"):
            app.lambda_handler(fixed_event, None)

    def test_error_log_is_structured_json(
        self, s3_environment, fixed_event, monkeypatch
    ):
        """
        ADR-004: Powertools Logger が ERROR レコードを出してから再送出すること。

        NOTE: capsys/capfd では捕捉できない。Powertools は import 時に
        StreamHandler を生成し propagate=False で動くため、
        ロガーへ直接ハンドラを付けてレコードを掴むのが最も確実。
        """
        import logging

        def boom(url):
            raise RuntimeError("Network down")
        monkeypatch.setattr("app.feedparser.parse", boom)

        captured_records = []

        class _CaptureHandler(logging.Handler):
            def emit(self, record):
                captured_records.append(record)

        handler = _CaptureHandler()
        app.logger.addHandler(handler)
        try:
            with pytest.raises(RuntimeError):
                app.lambda_handler(fixed_event, None)
        finally:
            app.logger.removeHandler(handler)

        errors = [r for r in captured_records if r.levelname == "ERROR"]
        assert errors, "Powertools Logger must emit an ERROR record before re-raising"

        record = errors[-1]
        assert getattr(record, "error_type", None) == "RuntimeError"
        assert getattr(record, "error_message", None) == "Network down"
        assert getattr(record, "event_id", None) == fixed_event["id"]
        assert record.exc_info is not None, \
            "logger.exception() must attach the stack trace"