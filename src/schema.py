"""
スキーマバージョンと互換性の管理。

ADR-012: スキーマ変更を「壊れる」「壊れない」に分類する。

背景:
    app.py には SCHEMA_VERSION = "2.0" という定数があるが、
    **これが上がったときに何が起きるかが定義されていない**。

    S3 には過去のバージョンで書かれた JSON が残り続ける。
    Athena でクエリすれば、新旧が同じテーブルとして読まれる——
    列が増えていれば古い行は NULL、型が変われば読み取りが落ちる。

    姉妹プロジェクトの item 105 で GCP 側に実装した
    「後方互換か破壊的か」の分類を、S3 上の JSON スキーマへ適用する。

    BigQuery と違い S3 + Athena にはスキーマ強制が無いため、
    **壊れた書き込みはエラーにならず、クエリ時に初めて露見する**。
    だからこそ書き込み側で判定する必要がある。
"""
from __future__ import annotations

# 現行スキーマの必須フィールド。app.py の build_payload と対応する。
REQUIRED_FIELDS = {
    "schema_version",
    "fetched_at",
    "source_feed",
    "article_hash",
    "article",
}

# 記事オブジェクトの必須フィールド。
REQUIRED_ARTICLE_FIELDS = {"title", "link", "published", "source"}

# 安全な型の緩和。ここに無い遷移は全て破壊的とみなす——
# 許可リストにするのは、禁止リストでは新しい型が自動的に
# 「安全」と判定されてしまうため。
SAFE_TYPE_WIDENING = {
    "int": {"int", "float"},
    "float": {"float"},
    "str": {"str"},
    "bool": {"bool"},
    "NoneType": {"NoneType", "str", "int", "float", "bool"},
}


def parse_version(version: str) -> tuple[int, int]:
    """
    "2.0" を (2, 0) に変換する。

    不正な文字列では ValueError を投げる。バージョンは
    比較の基準であり、曖昧なまま進めれば判定そのものが無意味になる。
    """
    parts = version.split(".")
    if len(parts) != 2:
        raise ValueError(f"version must be MAJOR.MINOR, got {version!r}")
    return int(parts[0]), int(parts[1])


def is_major_bump(old: str, new: str) -> bool:
    """
    メジャーバージョンが上がったかを返す。

    メジャーが上がる = 破壊的変更を含むという約束である。
    この約束を守らなければ、バージョン番号は単なる飾りになる。
    """
    return parse_version(new)[0] > parse_version(old)[0]


def diff_payload_keys(old_payload: dict, new_payload: dict) -> dict:
    """
    2 つのペイロードのキー差分を返す。

    追加と削除を分けて返す。まとめて「変更あり」とすれば、
    安全な追加と破壊的な削除が同列になる。
    """
    old_keys = set(old_payload)
    new_keys = set(new_payload)

    return {
        "added": sorted(new_keys - old_keys),
        "removed": sorted(old_keys - new_keys),
    }


def diff_types(old_payload: dict, new_payload: dict) -> list[dict]:
    """共通キーのうち型が変わったものを返す。"""
    changes = []
    for key in sorted(set(old_payload) & set(new_payload)):
        old_type = type(old_payload[key]).__name__
        new_type = type(new_payload[key]).__name__
        if old_type != new_type:
            changes.append({"field": key, "from": old_type, "to": new_type})
    return changes


def is_type_widening(old_type: str, new_type: str) -> bool:
    """型変更が後方互換かを返す。未知の遷移は危険とみなす。"""
    return new_type in SAFE_TYPE_WIDENING.get(old_type, set())


def classify_payload_change(old_payload: dict, new_payload: dict) -> dict:
    """
    ペイロードの変更を後方互換と破壊的に分類する。

    S3 の JSON は読み取り時にスキーマが解決されるため、
    **書き込み時には何のエラーも出ない**。分類は書き込む側の責務になる。
    """
    keys = diff_payload_keys(old_payload, new_payload)

    compatible = [f"added field {k}" for k in keys["added"]]
    breaking = [
        f"removed field {k}: existing queries selecting it will return null"
        for k in keys["removed"]
    ]

    for change in diff_types(old_payload, new_payload):
        message = f"{change['field']}: {change['from']} -> {change['to']}"
        if is_type_widening(change["from"], change["to"]):
            compatible.append(f"widened type {message}")
        else:
            breaking.append(f"narrowed type {message}")

    return {"compatible": compatible, "breaking": breaking}


def validate_payload(payload: dict) -> list[str]:
    """
    ペイロードが現行スキーマを満たすか検証する。

    例外を投げずエラーのリストを返す。1 件の不備で
    バッチ全体を落とせば、正常な記事まで届かなくなる
    （ADR-003 の DLQ と同じ思想）。
    """
    errors = []

    missing = REQUIRED_FIELDS - set(payload)
    if missing:
        errors.append(f"missing top-level fields: {sorted(missing)}")

    article = payload.get("article")
    if not isinstance(article, dict):
        errors.append("article must be an object")
        return errors

    missing_article = REQUIRED_ARTICLE_FIELDS - set(article)
    if missing_article:
        errors.append(f"missing article fields: {sorted(missing_article)}")

    return errors


def decide_version_bump(old_payload: dict, new_payload: dict,
                        old_version: str, new_version: str) -> dict:
    """
    バージョン番号の妥当性を判定する。

    'ok'      : 変更内容とバージョンの上げ方が一致している
    'violation': 破壊的変更なのにメジャーを上げていない

    バージョン番号が実態と食い違えば、下流は
    「2.x なら読める」という前提でクエリを書き、静かに壊れる。
    """
    change = classify_payload_change(old_payload, new_payload)
    bumped = is_major_bump(old_version, new_version)

    if change["breaking"] and not bumped:
        return {
            "status": "violation",
            "reason": (
                f"breaking changes present but version stayed at major "
                f"{parse_version(old_version)[0]}: {change['breaking']}"
            ),
            **change,
        }

    return {"status": "ok", "reason": None, **change}