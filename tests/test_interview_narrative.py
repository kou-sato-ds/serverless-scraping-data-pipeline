"""
面接想定問答の実行可能化（AWS 側）。

🎯 【#92の横展開】GCP側で作った問答の仕組みを、本命リポジトリへ!

背景:
    姉妹プロジェクト Mastering-Data-Engineering-Foundations の item 92 で
    「想定問答をコードとして持ち、根拠の実在をテストで守る」仕組みを作った。

    しかしその6問のうち4問は **本リポジトリの実装** を語っている。
    にもかかわらず、こちら側には問答が無い——
    採用担当者が本命から入った場合、ADR索引はあっても
    「本人はこれをどう語るか」に辿り着けない。

    本ファイルは同じ構造を AWS 側へ持ち込む。
    evidence は ADR 番号であり、実ファイルの存在をテストで検証する。

実行方法:
    pytest tests/test_interview_narrative.py -v
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
DOCS_DIR = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"

SIBLING_REPO = "Mastering-Data-Engineering-Foundations"

# 🎯 【想定問答】各エントリは question / answer / adrs / concern を持つ。
#    adrs は実在する ADR 番号——語った内容の裏付けである。
NARRATIVES = [
    {
        "id": "why_rss_over_playwright",
        "question": "コスト削減の経験はありますか",
        "answer": (
            "Playwright によるヘッドレスブラウザ収集を RSS フィードへ移行し、"
            "実測で 99.2% のコスト削減を達成しました。"
            "ブラウザ起動には数百MBのメモリと数秒の実行時間が必要ですが、"
            "RSS は XML を取得してパースするだけで済みます。"
            "取得できる情報が減るトレードオフを受け入れた上での判断です。"
        ),
        "adrs": [1],
        "concern": "migration",
    },
    {
        "id": "retry_needs_idempotency",
        "question": "リトライを有効化する前に何を確認しますか",
        "answer": (
            "冪等性です。旧実装は S3 キーを実行時刻から生成していたため、"
            "リトライのたびに別のキーへ書き込まれ重複が増える状態でした。"
            "先に記事のURLと公開日時から SHA-256 で決定的にキーを導出する設計へ変更し、"
            "その上でリトライと DLQ を有効化しています。"
            "PR の順序そのものが設計判断でした。"
        ),
        "adrs": [2, 3],
        "concern": "idempotency",
    },
    {
        "id": "async_lambda_semantics",
        "question": "非同期 Lambda で失敗をどう扱いますか",
        "answer": (
            "例外を握りつぶさず再送出します。"
            "EventBridge 起動の Lambda は return 値ではなく例外の有無でしか"
            "失敗を判定しないため、statusCode 500 を返しても成功扱いになります。"
            "旧実装はまさにこの状態で、自動リトライも DLQ もメトリクスも"
            "一切発火しないサイレント失敗を起こしていました。"
        ),
        "adrs": [3],
        "concern": "fault_tolerance",
    },
    {
        "id": "correlation_id_choice",
        "question": "ログの相関IDに何を使いますか",
        "answer": (
            "EventBridge の event id です。"
            "aws_request_id は試行ごとに変わるため、"
            "リトライで発生した3回分のログを紐付けられません。"
            "event id はリトライ間で不変なので、"
            "同一イベントの全試行を1つのIDで追跡できます。"
            "冪等キーの源泉に event time を選んだのと同じ基準です。"
        ),
        "adrs": [4, 5],
        "concern": "observability",
    },
    {
        "id": "silent_failure_incident",
        "question": "障害に気づけなかった経験はありますか",
        "answer": (
            "あります。テストが1件も collect できない状態が2日間 main に残りました。"
            "CI は赤かったのですが、常に失敗する別のワークフローが紛れ込んでおり、"
            "赤信号が意味を失っていました。"
            "復旧後にポストモーテムを ADR へ残し、"
            "テストスイート自身が健全性を検証するガードを実装しています。"
        ),
        "adrs": [6, 7],
        "concern": "postmortem",
    },
        {
        "id": "docs_that_do_not_rot",
        "question": "ドキュメントの陳腐化にどう対処しますか",
        "answer": (
            "テストで守れる形にします。"
            "ADR は書けば残りますが、README から辿れなければ存在しないのと同じです。"
            "索引と実体の乖離を双方向に検証するテストを置き、"
            "ADR を追加して README を更新し忘れれば CI が赤くなるようにしました。"
            "さらに想定問答自体もコードとして持ち、"
            "全 ADR が少なくとも1つの回答でカバーされることを検証しています。"
            "実際にこの仕組み自身が初回実行で捕まりました。"
        ),
        "adrs": [8, 9],
        "concern": "documentation",
    },
]


def scan_adr_numbers() -> list:
    """docs/ に実在する ADR 番号を返す。"""
    numbers = []
    for path in sorted(DOCS_DIR.glob("ADR-*.md")):
        match = re.match(r"ADR-(\d+)", path.name)
        if match:
            numbers.append(int(match.group(1)))
    return sorted(numbers)


# ================================================================
# STAGE 1: 全ての根拠 ADR が実在すること ← 本ファイルの核心
#   語った内容の裏付けが無ければ、それは主張であって証拠ではない。
# ================================================================
def test_every_cited_adr_exists():
    existing = set(scan_adr_numbers())

    missing = []
    for n in NARRATIVES:
        for adr in n["adrs"]:
            if adr not in existing:
                missing.append(f"{n['id']}:ADR-{adr:03d}")

    assert not missing, (
        f"these narratives cite ADRs that do not exist: {missing}. "
        "An answer without evidence is a claim, not a demonstration."
    )


# ================================================================
# STAGE 2: 全エントリが必須フィールドを持つこと
# ================================================================
@pytest.mark.parametrize("field", ["id", "question", "answer", "adrs", "concern"])
def test_every_narrative_has_required_fields(field):
    missing = [n.get("id", "?") for n in NARRATIVES if not n.get(field)]

    assert not missing, f"these narratives lack {field!r}: {missing}"


# ================================================================
# STAGE 3: ID が重複しないこと
# ================================================================
def test_narrative_ids_are_unique():
    ids = [n["id"] for n in NARRATIVES]
    assert len(ids) == len(set(ids)), f"duplicate narrative ids: {ids}"


# ================================================================
# STAGE 4: 回答が十分な長さを持つこと
#   一言で終わる回答は、面接では「理解していない」と受け取られる。
# ================================================================
def test_answers_are_substantive():
    thin = [n["id"] for n in NARRATIVES if len(n["answer"]) < 60]

    assert not thin, (
        f"these answers are too short to demonstrate understanding: {thin}"
    )


# ================================================================
# STAGE 5: 全 ADR が少なくとも1つの問答でカバーされること
#   書いた設計判断を語れなければ、記録した意味が半減する。
# ================================================================
def test_every_adr_has_a_narrative():
    cited = {adr for n in NARRATIVES for adr in n["adrs"]}
    existing = set(scan_adr_numbers())

    unspoken = sorted(existing - cited)

    assert not unspoken, (
        f"these ADRs have no prepared answer: {[f'ADR-{a:03d}' for a in unspoken]}. "
        "A decision recorded but never explained is half-wasted effort."
    )


# ================================================================
# STAGE 6: 姉妹プロジェクトを説明する問答があること
#   AWS/GCP 対称構造はポートフォリオの核であり、
#   問われた時に語れなければ伝わらない。
# ================================================================
def test_readme_mentions_the_sibling_for_context():
    readme = README.read_text(encoding="utf-8")

    assert SIBLING_REPO in readme, (
        "the GCP counterpart must be discoverable; an interviewer who asks "
        "about the symmetry needs somewhere to look"
    )


# ================================================================
# STAGE 7: 核心の関心が問答を持つこと
# ================================================================
@pytest.mark.parametrize("concern", [
    "idempotency", "fault_tolerance", "observability", "postmortem",
])
def test_core_concerns_have_a_narrative(concern):
    covered = {n["concern"] for n in NARRATIVES}

    assert concern in covered, (
        f"{concern} is central to this repository but has no prepared answer"
    )