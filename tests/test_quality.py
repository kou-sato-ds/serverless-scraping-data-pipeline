"""
ADR-010: データ品質検証のテスト。

境界値を明示的に固定する。閾値の 1 つ手前と 1 つ先を両方テストしなければ、
「>」と「>=」の取り違えは検知できない。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import quality


def _article(**overrides):
    base = {
        "title": "サンプル記事",
        "link": "https://example.com/a",
        "published": "2026-09-06T00:00:00+00:00",
        "source": "Example News",
    }
    base.update(overrides)
    return base


class TestEmptyRate:
    def test_empty_string_counts_as_missing(self):
        """RSS では「タグはあるが中身が空」が日常的に起きる。"""
        articles = [
            _article(title="あり"),
            _article(title=""),
            _article(title=None),
            _article(title="あり"),
        ]
        assert quality.compute_empty_rate(articles, "title") == 0.5

    def test_empty_input_returns_zero(self):
        assert quality.compute_empty_rate([], "title") == 0.0


class TestDuplicateLinks:
    def test_duplicate_link_is_detected(self):
        articles = [
            _article(link="https://example.com/a"),
            _article(link="https://example.com/a"),
            _article(link="https://example.com/b"),
        ]
        assert quality.find_duplicate_links(articles) == ["https://example.com/a"]

    def test_unique_links_report_nothing(self):
        articles = [
            _article(link="https://example.com/a"),
            _article(link="https://example.com/b"),
        ]
        assert quality.find_duplicate_links(articles) == []


class TestArticleCountThreshold:
    @pytest.mark.parametrize("count,expected", [
        (0, True),
        (4, True),
        (5, False),   # 境界値ちょうどは通す
        (100, False),
    ])
    def test_boundary(self, count, expected):
        assert quality.is_article_count_low(count) is expected


class TestQualityReport:
    def test_healthy_feed_reports_no_issues(self):
        articles = [
            _article(link=f"https://example.com/{i}") for i in range(20)
        ]
        report = quality.check_feed_quality(articles)

        assert quality.summarise_quality_issues(report) == []

    def test_degraded_feed_reports_issues(self):
        articles = [
            _article(title="", link=f"https://example.com/{i}")
            for i in range(10)
        ]
        report = quality.check_feed_quality(articles)
        issues = quality.summarise_quality_issues(report)

        assert any("empty title rate" in i for i in issues)

    def test_low_article_count_is_reported(self):
        report = quality.check_feed_quality([_article()])
        issues = quality.summarise_quality_issues(report)

        assert any("article count dropped" in i for i in issues)

    def test_missing_published_is_reported(self):
        articles = [
            _article(published=None, link=f"https://example.com/{i}")
            for i in range(10)
        ]
        report = quality.check_feed_quality(articles)
        issues = quality.summarise_quality_issues(report)

        assert any("missing published rate" in i for i in issues)

    def test_check_reports_rather_than_raises(self):
        """1 つの異常で全体を止めれば、正常な記事まで届かなくなる。"""
        broken = [{}, {"title": ""}, _article()]
        report = quality.check_feed_quality(broken)

        assert report["article_count"] == 3

    def test_empty_feed_does_not_crash(self):
        report = quality.check_feed_quality([])

        assert report["article_count"] == 0
        assert report["duplicate_links"] == []


class TestLogFields:
    def test_log_fields_include_counts_even_when_healthy(self):
        """無音は「正常」と「未実行」を区別できない。"""
        articles = [_article(link=f"https://example.com/{i}") for i in range(10)]
        report = quality.check_feed_quality(articles)
        fields = quality.build_quality_log_fields(report, [])

        assert fields["article_count"] == 10
        assert fields["quality_issue_count"] == 0

    def test_log_fields_carry_the_issue_list(self):
        report = quality.check_feed_quality([_article()])
        issues = quality.summarise_quality_issues(report)
        fields = quality.build_quality_log_fields(report, issues)

        assert fields["quality_issue_count"] == len(issues)
        assert fields["quality_issues"] == issues