# Analysis 03: キーワード出現頻度の時系列推移

## 🎯 ビジネスの問い
> 「\"AI\" \"中国\" \"選挙\" 等の話題は時間とともに増減しているか?」
> 「ある事件の前後で、関連キーワードの言及数はどう変化したか?」

## 📊 想定読者
- 広報・PRのトレンド追跡担当
- 市況・政治ウォッチャー
- データジャーナリスト
- 投資判断のためのニュース感応度モニタリング

## 🔍 SQL クエリ

\`\`\`sql
SELECT
    date_parse(concat(year, '-', month, '-', day), '%Y-%m-%d') AS collection_date,
    SUM(CASE WHEN LOWER(article.title) LIKE '%ai%'    THEN 1 ELSE 0 END) AS ai_mentions,
    SUM(CASE WHEN article.title LIKE '%中国%'         THEN 1 ELSE 0 END) AS china_mentions,
    SUM(CASE WHEN article.title LIKE '%選挙%'         THEN 1 ELSE 0 END) AS election_mentions,
    SUM(CASE WHEN article.title LIKE '%株価%'         THEN 1 ELSE 0 END) AS stock_mentions,
    COUNT(*) AS total_articles
FROM google_news_rss,
     UNNEST(articles) AS t(article)
WHERE year = '2026' AND month = '05'
GROUP BY year, month, day
ORDER BY collection_date;
\`\`\`

## 📈 期待される結果スキーマ

| collection_date | ai_mentions | china_mentions | election_mentions | stock_mentions | total |
|---|---|---|---|---|---|
| 2026-05-01 | 12 | 8 | 3 | 5 | 720 |
| 2026-05-02 | 15 | 11 | 4 | 6 | 715 |
| ... | ... | ... | ... | ... | ... |

## 💡 得られる洞察例
- 特定の事件 (例: 選挙告示日、市場暴落日) でのキーワード急増を可視化
- 時系列を matplotlib 等でチャート化すれば、**1枚で「データから何が言えるか」を語れる**
- 後段の機械学習タスク (ニュース感情分析、トピックモデリング) の入力データになる

## 📌 デプロイ後の実測値はここに追記予定
(時系列グラフの画像 + 所見をここに追加。週末デプロイ後)
