# ============================================================
# Playwright on AWS Lambda (Container Image)
# Base: python:3.9-slim (Debian系の軽量イメージ)
# Lambda Runtime Interface Client (awslambdaric) を組み込んで
# Lambda コンテナとして起動できるようにする。
# ============================================================
FROM python:3.9-slim

# ------------------------------------------------------------
# システム依存ライブラリ (Chromium 実行に必要)
# 参照: https://playwright.dev/python/docs/docker
#   - libnss3 / libnspr4    : NSS (Network Security Services)
#   - libatk1.0-0 / libatk-bridge2.0-0 / libatspi2.0-0 : Accessibility
#   - libcups2              : Common UNIX Printing System
#   - libdrm2 / libgbm1     : GPU/グラフィックスバッファ
#   - libxkbcommon0 / libxcomposite1 / libxdamage1 / libxfixes3 / libxrandr2
#                           : X Window System 関連
#   - libpango-1.0-0 / libcairo2 : テキスト/2Dグラフィックス
#   - libasound2            : 音声 (Chromium が依存)
#   - fonts-liberation / fonts-noto-cjk : 日本語含むフォント
# ------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libxcb1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    fonts-liberation \
    fonts-noto-cjk \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ------------------------------------------------------------
# Playwright ブラウザのインストール先を明示
# (Lambda の /tmp 以外は読み取り専用なので、ビルド時に固定パスへ展開)
# ------------------------------------------------------------
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LAMBDA_TASK_ROOT=/var/task

WORKDIR ${LAMBDA_TASK_ROOT}

# ------------------------------------------------------------
# Python 依存パッケージのインストール
#   - playwright       : スクレイピング本体
#   - boto3            : S3 アップロード
#   - awslambdaric     : Lambda Runtime Interface Client
#                        (slim ベースを Lambda として動かすために必須)
# ------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt awslambdaric

# ------------------------------------------------------------
# Chromium のみをインストール (Firefox/WebKit は不要)
# ------------------------------------------------------------
RUN python -m playwright install chromium

# ------------------------------------------------------------
# アプリケーションコード配置
# ------------------------------------------------------------
COPY src/ ${LAMBDA_TASK_ROOT}/

# ------------------------------------------------------------
# Lambda Runtime Interface Client をエントリポイントに設定
# CMD で指定したハンドラ (module.function) が呼び出される
# ------------------------------------------------------------
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["app.lambda_handler"]
