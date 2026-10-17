"""
ADR-018: app.py から使われていない src の部品を数え、増やさない。

テストが通っていても、lambda_handler から呼ばれていなければ本番では動いていない。
書いたこと(stated)と動いていること(demonstrated)の差を、ここで数える。

一覧は縮む方向にしか動かさない:
  - 新しい部品を足したら、組み込むか、理由(ADR)付きで一覧に載せる
  - 組み込んだら一覧から外す(外し忘れはテストが教える)
"""
import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "src"
DOCS = ROOT / "docs"

# 2026-10-13 時点で app.py から使われていない部品と、その理由を書いた ADR。
KNOWN_UNWIRED = {
    "schema": "ADR-012",
    "backfill": "ADR-013",
    "incremental": "ADR-014",
    "jst_partitions": "ADR-015",
    "run_reconciliation": "ADR-016",
}


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def reachable_from_app() -> set[str]:
    """app.py から直接・間接に import される src の部品。"""
    seen: set[str] = set()
    todo = ["app"]
    while todo:
        mod = todo.pop()
        path = SRC / f"{mod}.py"
        if mod in seen or not path.exists():
            continue
        seen.add(mod)
        todo.extend(_imports(path))
    return seen


def unwired() -> set[str]:
    mods = {p.stem for p in SRC.glob("*.py") if p.stem not in ("app", "__init__")}
    return mods - reachable_from_app()


def test_no_new_unwired_modules():
    """新しい部品は、組み込むか、理由付きで一覧に載せる。黙って増やさない。"""
    new = sorted(unwired() - set(KNOWN_UNWIRED))
    assert not new, (
        f"these modules are not used by app.py and are not listed: {new}. "
        "Wire them into lambda_handler, or add them to KNOWN_UNWIRED with an ADR."
    )


def test_wired_modules_are_removed_from_the_list():
    """組み込んだら一覧から外す。一覧は縮む方向にしか動かない。"""
    stale = sorted(set(KNOWN_UNWIRED) - unwired())
    assert not stale, f"these are now used by app.py; remove them from KNOWN_UNWIRED: {stale}"


def test_every_unwired_module_cites_an_existing_adr():
    """未接続の理由は、実在する ADR に書かれていなければならない。"""
    missing = [
        f"{mod}: {adr}" for mod, adr in KNOWN_UNWIRED.items()
        if not list(DOCS.glob(f"{adr}-*.md"))
    ]
    assert not missing, f"these ADRs do not exist: {missing}"