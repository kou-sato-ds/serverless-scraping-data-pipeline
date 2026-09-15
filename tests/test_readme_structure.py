"""
README の Markdown 構造検証。

ADR-011: 見出しが見出しとして描画されるかを検証する。

背景:
    姉妹プロジェクトで、45個の見出しが `> ` 付きで書かれており
    GitHub 上では引用ブロック内の文字列として沈んでいた。
    右側の目次には1つも載らず、1ヶ月以上気づかれなかった。

    さらに ADR 索引テーブルに 4 列の行が混入していた——
    それは **本リポジトリの ADR 索引から貼り間違えたもの** だった。
    つまり同じ操作を両方のリポジトリで行っている。

    ADR-006 に「壊れたことより、壊れたと気づけない仕組みの方が危険」
    と書いたが、今回はそれがドキュメントで再現した。
    テストが守っていたのは中身であって、見た目ではなかった。

実行方法:
    pytest tests/test_readme_structure.py -v
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
README = REPO_ROOT / "README.md"

# ADR 索引テーブルの列数。| の本数で数える。
ADR_TABLE_PIPES = 5  # | ADR | 関心 | 内容 | 解決した課題 |


def read_lines() -> list[str]:
    return README.read_text(encoding="utf-8").splitlines()


def find_quoted_headings(lines: list[str]) -> list[dict]:
    """
    引用ブロック内の見出しを検出する。

    `> ## タイトル` は GitHub では見出しにならず、引用文として
    描画され目次にも載らない。エディタ上では違和感がないため、
    プレビューを見なければ気づけない。
    """
    return [
        {"line": i + 1, "text": line.strip()}
        for i, line in enumerate(lines)
        if re.match(r"^>\s*#{1,6}\s", line)
    ]


def extract_headings(lines: list[str]) -> list[dict]:
    """
    正しく描画される見出しだけを抽出する。

    コードブロック内の `#` はコメントであり見出しではない。
    含めれば件数が膨らみ、節が消えたことを検知できなくなる。
    """
    headings = []
    in_code_block = False

    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        match = re.match(r"^(#{1,6})\s+(.*)", line)
        if match:
            headings.append({
                "line": i + 1,
                "level": len(match.group(1)),
                "text": match.group(2).strip(),
            })
    return headings


def find_adr_table_rows(lines: list[str]) -> list[dict]:
    """ADR 索引テーブルの行を抽出する。"""
    return [
        {"line": i + 1, "text": line.strip(), "pipes": line.count("|")}
        for i, line in enumerate(lines)
        if re.match(r"^\|\s*ADR-\d{3}\s*\|", line.strip())
    ]


class TestHeadings:
    def test_no_heading_is_trapped_in_a_blockquote(self):
        """姉妹プロジェクトで45件放置された事故を、こちらでも守る。"""
        quoted = find_quoted_headings(read_lines())

        assert not quoted, (
            f"{len(quoted)} headings render as quoted text, not headings: "
            f"lines {[q['line'] for q in quoted][:5]}. They appear in no "
            "table of contents."
        )

    def test_readme_has_headings(self):
        headings = extract_headings(read_lines())

        assert len(headings) >= 5, (
            f"only {len(headings)} headings found; sections may have been "
            "deleted or swallowed by a code block"
        )

    def test_comments_inside_code_blocks_are_not_counted(self):
        sample = [
            "# Real Heading",
            "```python",
            "# a comment, not a heading",
            "```",
            "## Another Heading",
        ]
        texts = [h["text"] for h in extract_headings(sample)]

        assert texts == ["Real Heading", "Another Heading"]


class TestAdrIndexTable:
    def test_every_adr_row_has_the_same_column_count(self):
        """
        姉妹プロジェクトで検知された事故と同種のもの。
        Markdown は列数が合わなくてもエラーを出さず、崩れた表を描画する。
        """
        rows = find_adr_table_rows(read_lines())

        assert rows, "the ADR index table must exist"

        wrong = [r for r in rows if r["pipes"] != ADR_TABLE_PIPES]

        assert not wrong, (
            f"these ADR rows have {[r['pipes'] for r in wrong]} pipes instead "
            f"of {ADR_TABLE_PIPES}, at lines {[r['line'] for r in wrong]}. "
            "A malformed row renders as a broken table without any error."
        )

    def test_adr_rows_are_consecutive(self):
        """
        索引の行が離れていれば、間に別の内容が挟まっている。
        表として描画されなくなる。
        """
        rows = find_adr_table_rows(read_lines())
        numbers = [r["line"] for r in rows]

        gaps = [
            (numbers[i], numbers[i + 1])
            for i in range(len(numbers) - 1)
            if numbers[i + 1] - numbers[i] != 1
        ]

        assert not gaps, (
            f"ADR index rows are not consecutive: {gaps}. Something is "
            "inserted between them, breaking the table."
        )

    def test_no_duplicate_adr_rows(self):
        """
        姉妹プロジェクトでは meta と data_quality が2回ずつ書かれていた。
        差し替えたつもりが追記になっていた形である。
        """
        rows = find_adr_table_rows(read_lines())
        adr_ids = [re.match(r"^\|\s*(ADR-\d{3})", r["text"]).group(1) for r in rows]

        duplicates = sorted({a for a in adr_ids if adr_ids.count(a) > 1})

        assert not duplicates, (
            f"these ADRs appear more than once in the index: {duplicates}. "
            "A replacement that silently became an append."
        )


class TestSiblingLink:
    def test_sibling_project_link_survives(self):
        """対称構造の導線が編集で失われていないか。"""
        content = README.read_text(encoding="utf-8")

        assert "Mastering-Data-Engineering-Foundations" in content, (
            "the GCP counterpart must remain reachable from this README"
        )