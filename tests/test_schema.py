"""
ADR-012: スキーマ互換性のテスト。

バージョン番号が実態と一致することを機械的に守る。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import schema


def _payload(**overrides):
    base = {
        "schema_version": "2.0",
        "fetched_at": "2026-09-21T00:00:00+00:00",
        "source_feed": "https://example.com/rss",
        "article_hash": "abc123",
        "article": {
            "title": "サンプル",
            "link": "https://example.com/a",
            "published": "2026-09-20T00:00:00+00:00",
            "source": "Example News",
        },
    }
    base.update(overrides)
    return base


class TestVersionParsing:
    def test_valid_version_parses(self):
        assert schema.parse_version("2.0") == (2, 0)
        assert schema.parse_version("10.3") == (10, 3)

    @pytest.mark.parametrize("bad", ["2", "2.0.1", "v2.0", ""])
    def test_malformed_version_raises(self, bad):
        """曖昧なバージョンを許せば、判定そのものが無意味になる。"""
        with pytest.raises(ValueError):
            schema.parse_version(bad)

    def test_major_bump_detection(self):
        assert schema.is_major_bump("2.0", "3.0") is True
        assert schema.is_major_bump("2.0", "2.1") is False


class TestTypeWidening:
    @pytest.mark.parametrize("old,new", [("int", "float"), ("int", "int")])
    def test_widening_is_safe(self, old, new):
        assert schema.is_type_widening(old, new) is True

    @pytest.mark.parametrize("old,new", [("float", "int"), ("str", "int")])
    def test_narrowing_is_breaking(self, old, new):
        assert schema.is_type_widening(old, new) is False

    def test_unknown_type_is_treated_as_unsafe(self):
        """許可リストは知らないものを拒否する。"""
        assert schema.is_type_widening("bytes", "str") is False


class TestPayloadClassification:
    def test_added_field_is_compatible(self):
        old = _payload()
        new = _payload(category="tech")

        result = schema.classify_payload_change(old, new)

        assert result["breaking"] == []
        assert any("added field category" in c for c in result["compatible"])

    def test_removed_field_is_breaking(self):
        old = _payload()
        new = {k: v for k, v in old.items() if k != "source_feed"}

        result = schema.classify_payload_change(old, new)

        assert any("removed field source_feed" in b for b in result["breaking"])

    def test_type_narrowing_is_breaking(self):
        old = _payload(article_hash="abc")
        new = _payload(article_hash=123)

        result = schema.classify_payload_change(old, new)

        assert any("narrowed type" in b for b in result["breaking"])


class TestPayloadValidation:
    def test_valid_payload_has_no_errors(self):
        assert schema.validate_payload(_payload()) == []

    def test_missing_top_level_field_is_reported(self):
        payload = _payload()
        del payload["article_hash"]

        errors = schema.validate_payload(payload)

        assert any("article_hash" in e for e in errors)

    def test_missing_article_field_is_reported(self):
        payload = _payload()
        del payload["article"]["link"]

        errors = schema.validate_payload(payload)

        assert any("link" in e for e in errors)

    def test_non_dict_article_is_reported(self):
        errors = schema.validate_payload(_payload(article="not an object"))

        assert any("must be an object" in e for e in errors)

    def test_validation_reports_rather_than_raises(self):
        """1件の不備でバッチ全体を落とせば、正常な記事まで届かない。"""
        errors = schema.validate_payload({})

        assert errors
        assert isinstance(errors, list)


class TestVersionBumpDecision:
    def test_compatible_change_without_bump_is_ok(self):
        old = _payload()
        new = _payload(category="tech")

        decision = schema.decide_version_bump(old, new, "2.0", "2.1")

        assert decision["status"] == "ok"

    def test_breaking_change_without_major_bump_is_a_violation(self):
        """
        バージョンが実態と食い違えば、下流は「2.x なら読める」という
        前提でクエリを書き、静かに壊れる。
        """
        old = _payload()
        new = {k: v for k, v in old.items() if k != "source_feed"}

        decision = schema.decide_version_bump(old, new, "2.0", "2.1")

        assert decision["status"] == "violation"
        assert "source_feed" in decision["reason"]

    def test_breaking_change_with_major_bump_is_ok(self):
        old = _payload()
        new = {k: v for k, v in old.items() if k != "source_feed"}

        decision = schema.decide_version_bump(old, new, "2.0", "3.0")

        assert decision["status"] == "ok"
        assert decision["breaking"], "the change is still reported as breaking"