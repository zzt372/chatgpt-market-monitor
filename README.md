# chatgpt-market-monitor

Market-data producers used by the ChatGPT market-monitor automation.

Each monitor is isolated in its own directory and GitHub Actions workflow. A failure in one producer does not block the others.

## Monitors

| Monitor | Output | Primary source |
|---|---|---|
| Nasdaq-100 Forward P/E | `monitors/nasdaq-forward-pe/latest.json` | Trendonify dedicated page via DuckDuckGo Lite search index |
| CNN Fear & Greed | `monitors/cnn-fear-greed/latest.json` | CNN official JSON |
| Nasdaq-100 drawdown from closing ATH | `monitors/nasdaq-drawdown/drawdown.json` | Nasdaq official historical API |

## Layout

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

The workflows keep last-known-good JSON on acquisition or validation failure and commit only validated output.
