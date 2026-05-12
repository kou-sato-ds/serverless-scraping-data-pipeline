# Analytics Queries (Athena / Glue)

Phase B-3 デプロイ後に Athena で実行する SQL クエリを格納しています。
各クエリは**ビジネス的な問い → SQL → 期待スキーマ → 得られる洞察** の構造で記述しています。

## 状態

| # | クエリ | 目的 | 状態 |
|---|---|---|---|
| 01 | [媒体別ランキング](01-source-ranking.md) | どの媒体が"今"発信力を持つか | 🔵 設計済・デプロイ待ち |
| 02 | [時間帯別の流量](02-hourly-volume.md) | ニュース発生のピーク時間帯は | 🔵 設計済・デプロイ待ち |
| 03 | [キーワード時系列](03-keyword-frequency.md) | 注目トピックの推移 | 🔵 設計済・デプロイ待ち |

## スキーマ前提

Glue Crawler が自動検出するスキーマ:

\`\`\`
google_news_rss (
    fetched_at     STRING,
    source_feed    STRING,
    count          BIGINT,
    articles       ARRAY<STRUCT
                       title:STRING,
                       link:STRING,
                       published:STRING,
                       source:STRING
                  >>
)
PARTITIONED BY (year, month, day, hour)   -- Hive 自動検出
\`\`\`

**ポイント**: `articles` がネストされた配列構造なので、個別記事を扱うには
\`UNNEST(articles)\` で行に展開する必要があります。これは Athena (Presto/Trino) の
標準パターン。
