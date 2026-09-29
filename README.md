# chatgpt-market-monitor

ChatGPTの「マーケット指標監視」タスクで使用する、市場データ取得用リポジトリです。

各指標はディレクトリとGitHub Actions workflowを分離しており、1つの取得処理が失敗しても他の指標には影響しない構成にしています。

## 監視対象

| 指標 | 出力JSON | 主な取得元 |
|---|---|---|
| NASDAQ100 Forward P/E | `monitors/nasdaq-forward-pe/latest.json` | Trendonify専用ページのDuckDuckGo Lite検索インデックス |
| CNN Fear & Greed | `monitors/cnn-fear-greed/latest.json` | CNN公式JSON |
| NASDAQ100 終値ATHからの下落率 | `monitors/nasdaq-drawdown/drawdown.json` | Nasdaq公式ヒストリカルAPI |

## ディレクトリ構成

```text
monitors/
  nasdaq-forward-pe/
    fetch.py
    test_fetch.py
    latest.json
  cnn-fear-greed/
    fetch.py
    latest.json
  nasdaq-drawdown/
    drawdown.py
    test_drawdown.py
    drawdown.json

.github/workflows/
  nasdaq-forward-pe.yml
  nasdaq-forward-pe-watchdog.yml
  cnn-fear-greed.yml
  cnn-fear-greed-watchdog.yml
  nasdaq-drawdown.yml
```

## 運用方針

- 各指標は独立したworkflowで更新します。
- 取得または検証に失敗した場合は、最後に正常取得できたJSONを保持します。
- 検証を通過したデータだけをcommitします。
- Forward P/EとCNN Fear & Greedには独立Watchdogがあります。
- ChatGPT側の監視タスクは、このリポジトリ内のJSONだけを判定元として使用します。
- APIキーやトークンなどの秘密情報はリポジトリへ保存しません。

## ChatGPT側の通知条件

### NASDAQ100 Forward P/E

以下を新しく下回った場合に通知します。

- P/E 20.0倍
- P/E 19.0倍
- P/E 18.5倍
- 10年percentile 20%

### CNN Fear & Greed

Fear / Extreme Fear / Neutral / Greed / Extreme Greed の**カテゴリが変化した場合のみ**通知します。

### NASDAQ100 下落率

終値ベースの過去最高値（ATH）から、以下の下落率へ新しく到達した場合に通知します。

- -10%
- -15%
- -20%
- -30%

同じ閾値以下に留まっている間は重複通知せず、一度回復してから再び下抜いた場合に再通知します。
