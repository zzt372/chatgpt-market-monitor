# chatgpt-market-monitor

ChatGPTの「マーケット指標監視」タスクで使用する、市場データ取得用リポジトリです。

各指標はディレクトリとGitHub Actions workflowを分離しています。1つの取得処理が失敗しても、他の指標の更新・通知判定を止めません。NASDAQ100 Forward P/E監視は2026-10-18に廃止しました。

## 監視対象

| 指標 | 出力JSON | 主な取得元 |
|---|---|---|
| CNN Fear & Greed | `monitors/cnn-fear-greed/latest.json` | CNN公式JSON |
| NASDAQ100の直近252取引日最高終値からの下落率 | `monitors/nasdaq-drawdown/drawdown.json` | Nasdaq公式ヒストリカルAPI |
| SOXの直近252取引日最高終値からの下落率 | `monitors/sox-drawdown/drawdown.json` | Nasdaq公式ヒストリカルAPI |
| 浦和美園かいじゅう歯科 予約空き日 | `monitors/dental-availability/latest.json` | Stransa予約画面（Playwright） |

## 指数の通知条件

NASDAQ100とSOXは独立して判定し、通知済みの段階・再通知を開始できる状態も指数ごとに保存します。

| 段階 | NASDAQ100 | SOX |
|---|---:|---:|
| 1 | −10% | −20% |
| 2 | −15% | −25% |
| 3 | −20% | −30% |
| 4 | −25% | −35% |

下落率は次の式です。約1年の値動きを見るため、全期間の過去最高値ではなく、現在日を含む直近252取引日の終値を使用します。

```text
rolling_drawdown_pct = (current_close / rolling_high_close - 1) * 100
```

現在値には取引時間中の価格ではなく、確定した終値を使います。年初来騰落率や前日比とも異なります。閾値の判定には丸め前の `rolling_drawdown_pct` を使用し、表示時だけ丸めます。

通知条件の正本は `monitors/market-alerts/policy.json`、状態を使った判定の正本は `monitors/market-alerts/alerts.py` です。

- **初回は基準状態を保存するだけです。** 既に到達している段階を、新しく到達した通知として送信しません。
- **同じ下落局面の同じ段階は繰り返し通知しません。** その後さらに深い段階に到達した場合は、新しい段階を通知します。
- **複数の段階を一度に飛び越えた場合は、最も深い段階を1件だけ通知します。** 途中の段階も通知済みとして記録します。
- **最高終値からの下落率が−5%以上に回復したら、次の下落局面に備えて再設定します。** 例：−20%から−12%への回復だけでは通知履歴をリセットせず、−5%またはそれより高い水準までの回復が必要です。
- **古いデータ、未来の日付、不正なデータでは新しいシグナルを作りません。** そのデータによる通知履歴の更新・再設定もしません。取得時刻と市場データの対象日を分けて検証します。

これは購入の検討を始めるための通知条件で、最適な購入価格や底値を示す条件ではありません。

## CNN Fear & Greed

Fear / Extreme Fear / Neutral / Greed / Extreme Greed の**カテゴリが変わった場合だけ**、既存の独立通知を継続します。スコアだけの小さな変化では通知しません。

指数の下落通知にも、その時点で利用できるFear & Greedのスコア・カテゴリ・対象時刻を添えます。ただし、Fear & Greedを指数通知の必須条件にはしません。取得が止まっている場合はその旨を表示し、正常な指数通知は続けます。Fear & Greedのカテゴリ変更通知も、指数の閾値到達を条件にしません。

## データ形式と更新

指数のJSONは `schema_version: 2` で、主に次のフィールドを使用します。

| フィールド | 内容 |
|---|---|
| `symbol` | `NDX` または `SOX` |
| `current_close` / `current_date` | 最新の確定終値と、その取引日 |
| `window_sessions` | `252` |
| `rolling_high_close` / `rolling_high_date` | 直近252取引日の最高終値と、その取引日 |
| `rolling_drawdown_pct` | 最高終値からの下落率。判定前に丸めない |
| `fetched_at` | 実際に取得・検証した時刻 |

NASDAQ100の `ath_close` / `ath_date` / `drawdown_pct` は過去形式との比較用に残します。**新しい通知判定には使用しません。**

各指数は独立したworkflowで更新します。取得または検証に失敗した場合は最後の正常JSONを保持し、保存値を読み直しただけで取得時刻を進めません。新しいデータを検証できた場合だけ公開します。

GitHub Actionsの定期実行はベストエフォートであり、設定間隔どおりの実行を保証しません。CNN Fear & Greedの独立Watchdogは既存どおり運用します。APIキー・トークンなどの秘密情報はリポジトリへ保存しません。

## ディレクトリ構成

```text
monitors/
  index_drawdown.py
  test_index_drawdown.py
  cnn-fear-greed/
    fetch.py
    latest.json
  nasdaq-drawdown/
    drawdown.py
    drawdown.json
  sox-drawdown/
    drawdown.py
    drawdown.json
  market-alerts/
    policy.json
    alerts.py
    task-instructions.md
  dental-availability/
    check.mjs
    latest.json

.github/workflows/
  cnn-fear-greed.yml
  cnn-fear-greed-watchdog.yml
  nasdaq-drawdown.yml
  sox-drawdown.yml
  dental-availability.yml
```

## ChatGPTのスケジュールタスクへの反映

GitHubのコード・JSONを変更しても、既存ChatGPTタスクに保存された監視指示は自動で置き換わりません。`monitors/market-alerts/task-instructions.md` を使い、既存タスクの監視指示を更新してください。実行頻度と通知先は既存設定を維持します。

移行時は新しい判定方式の初回基準状態を作成し、従来の全期間最高値に基づく通知履歴を流用しません。指示の更新、状態の初期化、正常な新形式データの読取確認まで行って初めて、スケジュールタスクへの反映完了です。
