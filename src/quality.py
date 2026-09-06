"""
RSS記事のデータ品質検証。

ADR-010: パースは通るが分析を歪めるレコードを検知する。

背景:
    ADR-003 で例外伝播と DLQ を導入したが、それが捕まえるのは
    「取得に失敗した」場合だけである。

    RSS フィードは正常に返ってきているのに、中身が劣化している
    ケースは捕まらない:
      - title が空文字の記事が大量に混じる
      - 同一 link の記事が重複して配信される
      - published が全て欠落し、パーティションが fetched_at に寄る
      - 記事数が普段の 1/10 に激減する

    いずれも feedparser はエラーを出さず、S3 には書き込まれ、
    Athena のクエリは静かに歪んだ結果を返す。

    姉妹プロジェクトの item 97-99 で GCP 側に実装した検証を、
    RSS パイプラインの文脈へ移植する。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# 記事件数の下限。これを下回ればフィード側の異常を疑う。
MIN_EXPECTED_ARTICLES = 5

# タイトル欠落率の上限。超えれば上流の配信フォーマット変更を疑う。
MAX_EMPTY_TITLE_RATE = 0.1

# published 欠落率の上限。超えるとパーティションが fetched_at に寄り、
# 時系列分析が歪む。
MAX_MISSING_PUBLISHED_RATE = 0.3


def compute_empty_rate(articles: list[dict], field: str) -> float:
    """
    指定フィールドが空である割合を返す。

    None だけでなく空文字も欠落として数える。RSS では
    「タグは存在するが中身が空」というケースが日常的に発生する。
    """
    if not articles:
        return 0.0

    empty = sum(
        1 for a in articles
        if a.get(field) is None or a.get(field) == ""
    )
    return round(empty / len(articles), 4)


def find_duplicate_links(articles: list[dict]) -> list[str]:
    """
    重複している link を返す。

    ADR-002 の Content-Addressable キーは link と published から
    導出されるため、同一 link かつ同一 published の記事は同じ
    S3 キーへ収束する。つまり重複自体は無害だが、**フィード側で
    何かが起きている兆候**であるため検知はする。
    """
    seen: dict[str, int] = {}
    for a in articles:
        link = a.get("link")
        if link:
            seen[link] = seen.get(link, 0) + 1
    return sorted(link for link, count in seen.items() if count > 1)


def is_article_count_low(count: int, minimum: int = MIN_EXPECTED_ARTICLES) -> bool:
    """記事数が下限を下回ったか判定する。境界値では発火しない。"""
    return count < minimum


def check_feed_quality(articles: list[dict]) -> dict:
    """
    フィード全体の品質レポートを返す。

    例外を投げない理由:
        品質チェックは処理を止めるためではなく、記録するためのものである。
        タイトルが 2 割欠けていても、残り 8 割は分析に使える。
        ADR-003 の DLQ が「壊れたものだけ隔離し、他は通す」のと同じ思想。
    """
    return {
        "article_count": len(articles),
        "empty_title_rate": compute_empty_rate(articles, "title"),
        "missing_published_rate": compute_empty_rate(articles, "published"),
        "duplicate_links": find_duplicate_links(articles),
    }


def summarise_quality_issues(report: dict) -> list[str]:
    """
    レポートから対処が必要な項目だけを抜き出す。

    正常値の一覧ではなく、閾値を超えたものだけを返す。深夜に
    アラートを読む人間が必要とするのは「何が起きたか」であって
    「何が正常か」ではない。
    """
    issues = []

    if is_article_count_low(report["article_count"]):
        issues.append(
            f"article count dropped to {report['article_count']} "
            f"(expected at least {MIN_EXPECTED_ARTICLES})"
        )

    if report["empty_title_rate"] > MAX_EMPTY_TITLE_RATE:
        issues.append(
            f"empty title rate {report['empty_title_rate']:.1%} "
            f"exceeds {MAX_EMPTY_TITLE_RATE:.0%}"
        )

    if report["missing_published_rate"] > MAX_MISSING_PUBLISHED_RATE:
        issues.append(
            f"missing published rate {report['missing_published_rate']:.1%} "
            f"exceeds {MAX_MISSING_PUBLISHED_RATE:.0%}"
        )

    if report["duplicate_links"]:
        issues.append(
            f"{len(report['duplicate_links'])} duplicate links in the feed"
        )

    return issues


def build_quality_log_fields(report: dict, issues: list[str]) -> dict:
    """
    Powertools Logger の extra へ渡すフィールドを構築する。

    正常時も出力する。異常時だけログを出すと、無音が「正常」なのか
    「チェックが走っていない」のか区別できなくなる。
    """
    return {
        "article_count": report["article_count"],
        "empty_title_rate": report["empty_title_rate"],
        "missing_published_rate": report["missing_published_rate"],
        "duplicate_link_count": len(report["duplicate_links"]),
        "quality_issue_count": len(issues),
        "quality_issues": issues,
    }