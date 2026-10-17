"""Official Nasdaq close history and trailing 252-session drawdown."""
import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

PAGE_SIZE = 5000
WINDOW = 252
NEW_YORK = ZoneInfo("America/New_York")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nasdaq.com/",
}


def utcnow():
    return datetime.now(timezone.utc)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric field required")
    if not math.isfinite(value):
        raise ValueError("non-finite value")
    return float(value)


def drawdown(close, peak):
    # Decimal division keeps exact threshold prices (e.g. 90/100) inclusive.
    return float((Decimal(str(close)) / Decimal(str(peak)) - 1) * 100)


class IndexCollector:
    def __init__(self, symbol, out):
        if symbol not in ("NDX", "SOX"):
            raise ValueError("unsupported symbol")
        self.symbol = symbol
        self.out = Path(out)
        self.base = f"https://api.nasdaq.com/api/quote/{symbol}/historical"

    def fetch_page(self, offset=0):
        params = {"assetclass": "index", "fromdate": "1985-01-01",
                  "todate": (utcnow().date() + timedelta(days=1)).isoformat(),
                  "limit": PAGE_SIZE, "offset": offset}
        req = Request(self.base + "?" + urlencode(params), headers=HEADERS)
        with urlopen(req, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Nasdaq HTTP {response.status}")
            obj = json.load(response)
        if obj.get("status", {}).get("rCode") != 200:
            raise ValueError("Nasdaq API rCode failure")
        data = obj.get("data")
        if not isinstance(data, dict) or data.get("symbol") != self.symbol:
            raise ValueError("symbol mismatch")
        table = data.get("tradesTable")
        rows = table.get("rows") if isinstance(table, dict) else None
        total = data.get("totalRecords")
        if not isinstance(rows, list) or type(total) is not int or total < WINDOW:
            raise ValueError("historical rows/totalRecords invalid")
        return rows, total

    def fetch_all(self):
        rows, total = self.fetch_page(0)
        rows = list(rows)
        while len(rows) < total:
            page, next_total = self.fetch_page(len(rows))
            if next_total != total:
                raise ValueError("totalRecords changed during pagination")
            if not page:
                raise ValueError("pagination ended early")
            rows.extend(page)
        if len(rows) != total:
            raise ValueError("incomplete history/count mismatch")
        return rows, total

    def build_payload(self, rows, total_records, fetched_at=None):
        stamp = fetched_at or utcnow()
        if stamp.tzinfo is None:
            raise ValueError("fetched_at timezone missing")
        if type(total_records) is not int or len(rows) != total_records:
            raise ValueError("incomplete history/count mismatch")
        local = stamp.astimezone(NEW_YORK)
        parsed, dates = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid history row")
            d = datetime.strptime(row["date"], "%m/%d/%Y").date()
            if d in dates:
                raise ValueError("duplicate history date")
            dates.add(d)
            if d > local.date():
                raise ValueError("future history date")
            raw = row["close"]
            if not isinstance(raw, str):
                raise ValueError("close must be string")
            close = float(raw.replace(",", "").strip())
            if not math.isfinite(close) or close <= 0:
                raise ValueError("invalid close")
            # Today's API row can still contain an intraday price.
            if d == local.date() and (local.hour, local.minute) < (16, 15):
                continue
            parsed.append((d, close))
        parsed.sort(reverse=True)
        if len(parsed) < WINDOW:
            raise ValueError("history too short for 252 finalized sessions")
        current_date, current_close = parsed[0]
        ath_date, ath_close = max(parsed, key=lambda x: (x[1], x[0]))
        window = parsed[:WINDOW]
        high_date, high_close = max(window, key=lambda x: (x[1], x[0]))
        return {
            "schema_version": 2, "ok": True, "symbol": self.symbol,
            "current_close": current_close, "current_date": current_date.isoformat(),
            "ath_close": ath_close, "ath_date": ath_date.isoformat(),
            "drawdown_pct": round(drawdown(current_close, ath_close), 4),
            "window_sessions": WINDOW, "window_start_date": window[-1][0].isoformat(),
            "rolling_high_close": high_close, "rolling_high_date": high_date.isoformat(),
            "rolling_drawdown_pct": drawdown(current_close, high_close),
            "total_records": total_records, "finalized_records": len(parsed),
            "fetched_at": stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "Nasdaq official API", "source_endpoint": self.base,
            "source_kind": "official-historical-close", "fetch_method": "nasdaq-api",
        }

    def validate_payload(self, payload, max_fetch_age_seconds=None, now=None):
        now = now or utcnow()
        if not isinstance(payload, dict) or payload.get("schema_version") != 2 or payload.get("ok") is not True:
            raise ValueError("schema/ok mismatch")
        for key, expected in {"symbol": self.symbol, "source": "Nasdaq official API",
                              "source_endpoint": self.base, "source_kind": "official-historical-close",
                              "fetch_method": "nasdaq-api", "window_sessions": WINDOW}.items():
            if payload.get(key) != expected:
                raise ValueError(f"{key} mismatch")
        current, ath, high = (number(payload[k]) for k in ("current_close", "ath_close", "rolling_high_close"))
        if not 0 < current <= high <= ath:
            raise ValueError("invalid close/ATH/rolling high")
        for key, peak, tolerance in (("drawdown_pct", ath, 0.000051), ("rolling_drawdown_pct", high, 1e-9)):
            if abs(number(payload[key]) - (current / peak - 1) * 100) > tolerance:
                raise ValueError("drawdown mismatch")
        current_date, ath_date, high_date, start = (datetime.strptime(payload[k], "%Y-%m-%d").date()
            for k in ("current_date", "ath_date", "rolling_high_date", "window_start_date"))
        local = now.astimezone(NEW_YORK)
        if current_date > local.date() or (current_date == local.date() and (local.hour, local.minute) < (16, 15)):
            raise ValueError("unfinalized/future current_date")
        if current_date < local.date() - timedelta(days=7):
            raise ValueError("stale current_date")
        if not start <= high_date <= current_date or ath_date > current_date:
            raise ValueError("invalid history dates")
        if (current_date - start).days < WINDOW - 1:
            raise ValueError("invalid window dates")
        for key in ("total_records", "finalized_records"):
            if type(payload.get(key)) is not int or payload[key] < WINDOW:
                raise ValueError("history too short")
        if payload["finalized_records"] > payload["total_records"]:
            raise ValueError("history count mismatch")
        fetched = datetime.fromisoformat(payload["fetched_at"].replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            raise ValueError("fetched_at timezone missing")
        age = (now - fetched.astimezone(timezone.utc)).total_seconds()
        if age < -600 or (max_fetch_age_seconds is not None and age > max_fetch_age_seconds):
            raise ValueError("fetched_at stale/future")
        return age

    def main(self, argv=None):
        parser = argparse.ArgumentParser()
        parser.add_argument("--check-latest", action="store_true")
        parser.add_argument("--max-fetch-age-seconds", type=int, default=7200)
        args = parser.parse_args(argv)
        try:
            if args.check_latest:
                payload = json.loads(self.out.read_text(encoding="utf-8"))
                age = self.validate_payload(payload, args.max_fetch_age_seconds)
                print(f"{self.symbol} healthy; fetched_age_seconds={age:.0f}")
            else:
                rows, total = self.fetch_all()
                payload = self.build_payload(rows, total)
                self.validate_payload(payload, 60)
                self.out.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.out.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                tmp.replace(self.out)
                print(json.dumps(payload))
            return 0
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
