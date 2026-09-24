"""ADR-014: 増分取得のテスト。"""
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import incremental

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _article(link, minutes=None):
    published = None if minutes is None else (T0 + timedelta(minutes=minutes)).isoformat()
    return {"title": "t", "link": link, "published": published, "source": "s"}


class FakeS3:
    """get_object / put_object だけを持つ最小の S3 スタブ。"""

    def __init__(self, error_code=None):
        self.objects = {}
        self.error_code = error_code

    def get_object(self, Bucket, Key):
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code}}, "GetObject")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body


class TestSelection:
    def test_first_run_takes_everything(self):
        articles = [_article("a", -500), _article("b", 0)]
        assert len(incremental.select_new_articles(articles, None)) == 2

    def test_late_article_within_lookback_is_kept(self):
        articles = [_article("late", -60)]
        assert len(incremental.select_new_articles(articles, T0)) == 1

    def test_article_older_than_lookback_is_skipped(self):
        articles = [_article("old", -300)]
        assert incremental.select_new_articles(articles, T0) == []

    def test_article_at_exact_watermark_is_kept(self):
        """> で切ると、同一時刻の未処理記事が永久に落ちる。"""
        articles = [_article("tie", 0)]
        kept = incremental.select_new_articles(articles, T0, lookback=timedelta(0))
        assert len(kept) == 1

    def test_article_without_published_is_kept(self):
        """判定できないものは捨てない。重複しても ADR-002 のキーで収束する。"""
        articles = [_article("unknown")]
        assert len(incremental.select_new_articles(articles, T0)) == 1


class TestWatermark:
    def test_never_moves_backwards(self):
        assert incremental.next_watermark([_article("late", -60)], T0) == T0

    def test_articles_without_dates_keep_the_watermark(self):
        assert incremental.next_watermark([_article("x")], T0) == T0

    def test_missing_state_means_first_run(self):
        assert incremental.load_watermark(FakeS3(), "bucket") is None

    def test_access_error_is_not_mistaken_for_first_run(self):
        """権限エラーを初回と誤認すれば、毎回全件取得が静かに続く。"""
        with pytest.raises(ClientError):
            incremental.load_watermark(FakeS3(error_code="AccessDenied"), "bucket")

    def test_saved_watermark_round_trips(self):
        s3 = FakeS3()
        incremental.save_watermark(s3, "bucket", T0)
        assert incremental.load_watermark(s3, "bucket") == T0


class TestRunIncrement:
    def test_success_advances_the_stored_watermark(self):
        s3 = FakeS3()
        uploaded = []

        result = incremental.run_increment(s3, "bucket", [_article("a", 30)], uploaded.extend)

        assert result["selected"] == 1
        assert incremental.load_watermark(s3, "bucket") == T0 + timedelta(minutes=30)

    def test_failed_upload_does_not_advance_the_watermark(self):
        """失敗後に進めれば、その区間は二度と取得されない。"""
        s3 = FakeS3()
        incremental.save_watermark(s3, "bucket", T0)

        def boom(batch):
            raise RuntimeError("S3 unavailable")

        with pytest.raises(RuntimeError):
            incremental.run_increment(s3, "bucket", [_article("a", 30)], boom)

        assert incremental.load_watermark(s3, "bucket") == T0