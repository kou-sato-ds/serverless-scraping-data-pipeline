"""
Pytest configuration and shared fixtures.

このファイルは pytest が自動で読み込むため、テスト本体より先に実行される。
src/ をインポートパスに加え、テスト用のダミー環境変数をセットしておくことで、
app モジュールの import 時に AWS 認証エラーや S3_BUCKET 未設定エラーを
発生させないようにする。
"""
import os
import sys

# src/ ディレクトリを PYTHONPATH に追加
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# テスト用ダミー認証情報 (boto3 が実 AWS を見に行かないようにする)
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-1")

# app.py 用の環境変数
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("S3_PREFIX", "test-prefix")
os.environ.setdefault("RSS_URL", "https://example.com/rss")
os.environ.setdefault("MAX_ARTICLES", "100")
os.environ.setdefault("SKIP_UPLOAD", "false")


import pytest


@pytest.fixture(autouse=True)
def reset_s3_client_cache():
    """
    各テストの後に lru_cache をクリアして、moto の差し替えを反映可能にする。
    """
    yield
    import app
    app.get_s3_client.cache_clear()
