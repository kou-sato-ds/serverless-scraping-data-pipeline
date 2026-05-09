"""
Unit and integration tests for the RSS collector Lambda.
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
# build_object_key
# ================================================================
class TestBuildObjectKey:
    def test_hive_compatible_format(self):
        dt = datetime(2026, 5, 9, 13, 19, 50, tzinfo=timezone.utc)
        key = app.build_object_key(dt)
        assert key == "test-prefix/year=2026/month=05/day=09/hour=13/131950.json"

    def test_zero_padding(self):
        dt = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        key = app.build_object_key(dt)
        assert "year=2026" in key
        assert "month=01" in key
        assert "day=02" in key
        assert "hour=03" in key
        assert key.endswith("030405.json")


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
# upload_to_s3 (moto で S3 をモック)
# ================================================================
class TestUploadToS3:
    def test_uploads_articles_with_correct_content_type(self, s3_environment, monkeypatch):
        monkeypatch.setattr("app.SKIP_UPLOAD", False)

        articles = [
            {
                "title": "テスト記事",
                "link": "https://example.com/1",
                "published": "2026-05-09T00:00:00+00:00",
                "source": "テストソース",
            }
        ]
        key = app.upload_to_s3(articles)

        assert key is not None
        assert "year=" in key and "month=" in key and "day=" in key

        obj = s3_environment.get_object(Bucket="test-bucket", Key=key)
        assert obj["ContentType"] == "application/json"

        body = json.loads(obj["Body"].read().decode("utf-8"))
        assert body["count"] == 1
        assert body["articles"][0]["title"] == "テスト記事"
        assert "fetched_at" in body
        assert "source_feed" in body

    def test_skips_upload_when_flag_set(self, s3_environment, monkeypatch):
        monkeypatch.setattr("app.SKIP_UPLOAD", True)
        result = app.upload_to_s3([{"title": "X", "link": "Y"}])
        assert result is None

    def test_raises_when_bucket_not_set(self, s3_environment, monkeypatch):
        monkeypatch.setattr("app.SKIP_UPLOAD", False)
        monkeypatch.setattr("app.S3_BUCKET", None)
        with pytest.raises(RuntimeError, match="S3_BUCKET"):
            app.upload_to_s3([{"title": "X", "link": "Y"}])


# ================================================================
# lambda_handler
# ================================================================
class TestLambdaHandler:
    def test_full_flow_returns_200(self, s3_environment, patched_feedparser, monkeypatch):
        monkeypatch.setattr("app.SKIP_UPLOAD", False)

        response = app.lambda_handler({}, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["count"] == 3
        assert body["bucket"] == "test-bucket"
        assert "key" in body

    def test_returns_204_when_no_articles(self, s3_environment, monkeypatch):
        class EmptyFeed:
            entries = []
            bozo = 0
        monkeypatch.setattr("app.feedparser.parse", lambda url: EmptyFeed())

        response = app.lambda_handler({}, None)
        assert response["statusCode"] == 204

    def test_returns_500_on_exception(self, s3_environment, monkeypatch):
        def boom(url):
            raise RuntimeError("Network down")
        monkeypatch.setattr("app.feedparser.parse", boom)

        response = app.lambda_handler({}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "Network down" in body["error"]
