# Analysis 02: 時間帯別の記事流量分布

## 🎯 ビジネスの問い
> 「1日のうちで最もニュースが発生しているのは何時帯か?」
> 「収集 Lambda が空回りしている時間帯はあるか? (コスト最適化のヒント)」

## 📊 想定読者
- データパイプラインの運用担当 (収集頻度の最適化判断)
- 速報メディアの編集担当
- マーケティング施策の配信タイミング設計

## 🔍 SQL クエリ

\`\`\`sql
SELECT
    hour AS hour_of_day,
    SUM(count) AS total_articles_collected,
    COUNT(DISTINCT day) AS unique_days_observed,
    ROUND(AVG(count), 1) AS avg_articles_per_collection
FROM google_news_rss
WHERE year = '2026' AND month = '05'
GROUP BY hour
ORDER BY hour;
\`\`\`

注: このクエリは UNNEST 不要 (トップレベルの \`count\` を使用)。
パーティションフィルタが効くため、スキャン量はごく小さい。

## 📈 期待される結果スキーマ

| hour_of_day | total_articles | unique_days | avg_per_collection |
|---|---|---|---|
| 00 | 540 | 18 | 30.0 |
| 01 | 525 | 18 | 29.2 |
| ... | ... | ... | ... |
| 23 | 612 | 18 | 34.0 |

## 💡 得られる洞察例
- 朝/昼/夜のニュース発生量パターン
- もし時間帯ごとの差が小さければ「**RSS は時刻に依存しない安定供給源**」と証明できる
- 収集頻度を 1h → 6h に間引いてもデータ価値が落ちないかの判断材料

## 📌 デプロイ後の実測値はここに追記予定
