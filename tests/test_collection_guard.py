"""
テスト収集の健全性ガード。

🎯 【ADR-006 の再発防止】「テストが0件でも気づけない」穴を塞ぐ!

背景:
    PR #6 のマージ後、tests/test_app.py に構文エラーが含まれ、
    テストが1件も collect できない状態が2日間 main に残った。

    CI は赤かったが、その赤は「常に失敗する ci.yml」に紛れて意味を失っていた。
    さらに --cov-fail-under=90 という設定があっても、
    **テストが0件ならカバレッジ計算に到達しない**ため、
    「件数が激減した」こと自体を検知する仕組みは存在しなかった。

    本ファイルは、テストスイート自身が自分の健全性を検証する。

実行方法:
    pytest tests/test_collection_guard.py -v
"""
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
APP_TEST = TESTS_DIR / "test_app.py"

# 🚨 現在の実測値は 43。ここを下回ったら「テストが消えた」ことを意味する。
#    件数を増やした時は、この下限も引き上げること。
MINIMUM_EXPECTED_TESTS = 40


def _count_test_functions(path: Path) -> int:
    """ファイル内の `def test_` の数を数える。"""
    text = path.read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if line.strip().startswith("def test_"))


# ================================================================
# STAGE 1: メインのテストファイルが構文的に正しいこと
#   ADR-006 の事故は `def  None():` という構文エラーだった。
#   collect 前に compile して、壊れていれば明示的に落とす。
# ================================================================
def test_main_test_file_is_syntactically_valid():
    import ast

    assert APP_TEST.exists(), "tests/test_app.py must exist"

    source = APP_TEST.read_text(encoding="utf-8")
    try:
        ast.parse(source, filename=str(APP_TEST))
    except SyntaxError as e:
        pytest.fail(
            f"tests/test_app.py has a syntax error at line {e.lineno}: {e.msg}. "
            "This is exactly the failure mode recorded in ADR-006, where the "
            "entire suite silently stopped running."
        )


# ================================================================
# STAGE 2: テスト件数が下限を割っていないこと
#   「壊れて0件」も「うっかり大量削除」も、同じアサーションで捕まえる。
# ================================================================
def test_suite_has_not_shrunk():
    count = _count_test_functions(APP_TEST)

    assert count >= MINIMUM_EXPECTED_TESTS, (
        f"tests/test_app.py defines only {count} tests, below the floor of "
        f"{MINIMUM_EXPECTED_TESTS}. Either tests were removed, or the file is "
        "broken. ADR-006 documents an incident where this count silently became 0."
    )


# ================================================================
# STAGE 3: 全てのテストファイルが読み込めること
#   将来ファイルが増えても、壊れた瞬間に検知される。
# ================================================================
def test_every_test_file_parses():
    import ast

    broken = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            broken.append(f"{path.name}:{e.lineno} {e.msg}")

    assert not broken, f"these test files cannot be parsed: {broken}"


# ================================================================
# STAGE 4: 文字化けが混入していないこと
#   ADR-005 で記録した通り、PowerShell 経由の編集で日本語が壊れる事故があった。
#   モジバケ特有のバイト列が現れたら落とす。
#
#   NOTE: このファイル自身は検知パターンを文字列として保持しているため、
#         検査対象から除外する。ガードが自分自身を誤検知しないための措置。
# ================================================================
MOJIBAKE_MARKERS = [
    "\u7e67",  # 繧
    "\u7e3a",  # 縺
    "\uff7d",  # ｽ
    "\uff7a",  # ｺ
]


@pytest.mark.parametrize("marker", MOJIBAKE_MARKERS)
def test_no_mojibake_in_test_files(marker):
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue  # 👉 自分自身はパターン定義を含むためスキップ
        if marker in path.read_text(encoding="utf-8"):
            offenders.append(path.name)

    assert not offenders, (
        f"mojibake marker U+{ord(marker):04X} found in {offenders}. "
        "See ADR-005: PowerShell's Get-Content|Set-Content corrupts UTF-8 "
        "Japanese text; use [System.IO.File]::ReadAllText with explicit encoding."
    )