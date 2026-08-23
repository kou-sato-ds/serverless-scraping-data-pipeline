"""
ADR索引の自己記述 — ドキュメントとREADMEの整合を守る。

🎯 【#90の横展開】学習ログで作った索引の仕組みを、本命リポジトリへ!

背景:
    本リポジトリには ADR-001〜007 と PR #1〜#13 が積み上がっているが、
    README がそれを案内していない。採用担当者が最初に開いた時
    「どの ADR から読めばよいか」が分からない。

    姉妹プロジェクト Mastering-Data-Engineering-Foundations の item 90 で
    「索引を手書きせずファイルシステムから生成する」仕組みを作った。
    同じ思想をここへ持ち込む。

    ただし本リポジトリの検証対象は README そのものである——
    ADR ファイルが存在するのに README から辿れなければ、
    書いた意味の半分が失われる。

実行方法:
    pytest tests/test_docs_index.py -v
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
DOCS_DIR = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"

# 🎯 各 ADR が扱う設計上の関心。README の索引と対応する。
ADR_CONCERNS = {
    1: "migration",        # Playwright -> RSS
    2: "idempotency",      # Content-Addressable keys
    3: "fault_tolerance",  # exception propagation + DLQ
    4: "observability",    # Powertools structured logging
    5: "observability",    # correlation id
    6: "postmortem",       # broken main incident
    7: "testing",          # collection guard
    8: "documentation",    # ADR index
}


def scan_adr_numbers() -> list:
    """
    🔍 docs/ をスキャンし、実在する ADR 番号を返す純粋関数。

    手書きのリストではなくファイルシステムを情報源にすることで、
    「README には書いたが ADR が無い」という乖離を防ぐ。
    """
    numbers = []
    for path in sorted(DOCS_DIR.glob("ADR-*.md")):
        match = re.match(r"ADR-(\d+)", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


# ================================================================
# STAGE 1: ADR ファイルが実在すること
# ================================================================
def test_adr_files_exist():
    assert DOCS_DIR.exists(), "docs/ directory must exist"

    numbers = scan_adr_numbers()
    assert len(numbers) >= 7, (
        f"only {len(numbers)} ADRs found: {numbers}. "
        "ADR-001 through ADR-007 should be present."
    )


# ================================================================
# STAGE 2: ADR 番号が連番であること
#   欠番があれば「削除されたのか、書き忘れたのか」が判別できない。
# ================================================================
def test_adr_numbers_are_contiguous():
    numbers = scan_adr_numbers()

    expected = list(range(1, len(numbers) + 1))
    assert numbers == expected, (
        f"ADR numbers are {numbers} but should be {expected}. "
        "A gap makes it ambiguous whether a decision was removed or never written."
    )


# ================================================================
# STAGE 3: 全 ADR が README から辿れること ← 本ファイルの核心
#   書いたのに案内されていない ADR は、存在しないのとほぼ同じ。
# ================================================================
def test_every_adr_is_linked_from_readme():
    readme = _readme_text()

    unlinked = []
    for n in scan_adr_numbers():
        if f"ADR-{n:03d}" not in readme:
            unlinked.append(f"ADR-{n:03d}")

    assert not unlinked, (
        f"these ADRs exist but are not referenced in README: {unlinked}. "
        "A decision record nobody can find is half-wasted effort."
    )


# ================================================================
# STAGE 4: README が参照する ADR が実在すること（逆方向）
#   #90 と同じく、乖離は双方向に起きうる。
# ================================================================
def test_readme_does_not_reference_missing_adrs():
    readme = _readme_text()
    existing = {f"ADR-{n:03d}" for n in scan_adr_numbers()}

    referenced = set(re.findall(r"ADR-\d{3}", readme))
    phantom = sorted(referenced - existing)

    assert not phantom, (
        f"README references {phantom}, which do not exist in docs/. "
        "A reader following the link would find nothing."
    )


# ================================================================
# STAGE 5: 各 ADR が Status を宣言していること
#   Status の無い ADR は「検討中なのか決定済みなのか」が分からない。
# ================================================================
def test_every_adr_declares_status():
    missing = []
    for path in sorted(DOCS_DIR.glob("ADR-*.md")):
        if "Status" not in path.read_text(encoding="utf-8"):
            missing.append(path.name)

    assert not missing, (
        f"these ADRs do not declare a Status: {missing}. "
        "Without it, a reader cannot tell a proposal from a decision."
    )


# ================================================================
# STAGE 6: 姉妹プロジェクトへの言及が README にあること
#   AWS/GCP 対称構造は本ポートフォリオの核であり、
#   片方からしか辿れなければ半分しか伝わらない。
# ================================================================
def test_readme_links_to_sibling_project():
    readme = _readme_text()

    assert "Mastering-Data-Engineering-Foundations" in readme, (
        "the GCP counterpart must be discoverable from here; the symmetry "
        "between the two repositories is what this portfolio demonstrates"
    )


# ================================================================
# STAGE 7: ADR が扱う関心の分類が実体と一致すること
# ================================================================
def test_concern_map_matches_existing_adrs():
    existing = set(scan_adr_numbers())
    declared = set(ADR_CONCERNS)

    assert declared == existing, (
        f"the concern map covers {sorted(declared)} but ADRs are "
        f"{sorted(existing)}. Every ADR needs a concern, and every concern "
        "needs an ADR."
    )