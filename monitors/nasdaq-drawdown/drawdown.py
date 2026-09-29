#!/usr/bin/env python3
import argparse
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "drawdown.json"
SCHEMA_VERSION = 1
SYMBOL = "NDX"
SOURCE = "Nasdaq official API"
SOURCE_KIND = "official-historical-close"
FETCH_METHOD = "nasdaq-api"
BASE = "https://api.nasdaq.com/api/quote/NDX/historical"
PAGE_SIZE = 5000
MAX_DATA_AGE_DAYS = 7

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaq.com/",
}


def utcnow():
    return datetime.now(timezone.utc)


def parse_date(text):
    return datetime.strptime(text, "%m/%d/%Y").date()


def parse_num(text):
    if not isinstance(text, str):
        raise ValueError("numeric field must be string")
    value = float(text.replace(",", "").strip())
    if not math.isfinite(value):
        raise ValueError("non-finite value")
    return value


def fetch_page(offset=0):
    today = utcnow().date()
    params = {
        "assetclass": "index",
        "fromdate": "1985-01-01",
        "todate": (today + timedelta(days=1)).isoformat(),
        "limit": PAGE_SIZE,
        "offset": offset,
    }
    url = BASE + "?" + urlencode(params)
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Nasdaq HTTP {response.status}")
        raw = response.read().decode("utf-8", "replace")
    obj = json.loads(raw)
    if obj.get("status", {}).get("rCode") != 200:
        raise RuntimeError("Nasdaq API rCode failure")
    data = obj.get("data")
    if not isinstance(data, dict) or data.get("symbol") != SYMBOL:
        raise ValueError("unexpected Nasdaq payload")
    table = data.get("tradesTable")
    rows = table.get("rows") if isinstance(table, dict) else None
    if not isinstance(rows, list):
        raise ValueError("historical rows missing")
    total = data.get("totalRecords")
    if not isinstance(total, int) or total < 1:
        raise ValueError("invalid totalRecords")
    return rows, total


def fetch_all():
    rows, total = fetch_page(0)
    all_rows = list(rows)
    offset = len(rows)
    while offset < total:
        page, total2 = fetch_page(offset)
        if total2 != total:
            raise ValueError("totalRecords changed during pagination")
        if not page:
            raise ValueError("pagination ended early")
        all_rows.extend(page)
        offset += len(page)
    if len(all_rows) < total:
        raise ValueError("incomplete history")
    return all_rows[:total], total


def build_payload(rows, total_records, fetched_at=None):
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = parse_date(row["date"])
        close = parse_num(row["close"])
        if close <= 0:
            raise ValueError("non-positive close")
        parsed.append((d, close))

    if not parsed:
        raise ValueError("no valid history")

    parsed.sort(key=lambda x: x[0], reverse=True)
    current_date, current_close = parsed[0]
    ath_date, ath_close = max(parsed, key=lambda x: (x[1], x[0]))
    if current_close > ath_close:
        raise ValueError("current close exceeds ATH")

    dd = (current_close / ath_close - 1.0) * 100.0
    stamp = fetched_at or utcnow()

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "symbol": SYMBOL,
        "current_close": round(current_close, 4),
        "current_date": current_date.isoformat(),
        "ath_close": round(ath_close, 4),
        "ath_date": ath_date.isoformat(),
        "drawdown_pct": round(dd, 4),
        "total_records": int(total_records),
        "fetched_at": stamp.isoformat().replace("+00:00", "Z"),
        "source": SOURCE,
        "source_endpoint": BASE,
        "source_kind": SOURCE_KIND,
        "fetch_method": FETCH_METHOD,
    }


def validate_payload(payload, max_fetch_age_seconds=None, now=None):
    if not isinstance(payload, dict):
        raise ValueError("payload must be object")
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("ok") is not True:
        raise ValueError("schema/ok mismatch")
    if payload.get("symbol") != SYMBOL:
        raise ValueError("symbol mismatch")
    if payload.get("source") != SOURCE:
        raise ValueError("source mismatch")
    if payload.get("source_endpoint") != BASE:
        raise ValueError("endpoint mismatch")
    if payload.get("source_kind") != SOURCE_KIND:
        raise ValueError("source_kind mismatch")
    if payload.get("fetch_method") != FETCH_METHOD:
        raise ValueError("fetch_method mismatch")

    current_close = float(payload["current_close"])
    ath_close = float(payload["ath_close"])
    dd = float(payload["drawdown_pct"])
    if not all(math.isfinite(v) for v in (current_close, ath_close, dd)):
        raise ValueError("non-finite payload value")
    if current_close <= 0 or ath_close <= 0 or current_close > ath_close:
        raise ValueError("invalid close/ATH")
    expected = (current_close / ath_close - 1.0) * 100.0
    if abs(dd - expected) > 0.01:
        raise ValueError("drawdown mismatch")
    if dd > 0.0001 or dd < -100:
        raise ValueError("invalid drawdown")

    current_date = datetime.strptime(payload["current_date"], "%Y-%m-%d").date()
    ath_date = datetime.strptime(payload["ath_date"], "%Y-%m-%d").date()
    today = (now or utcnow()).date()
    if current_date > today + timedelta(days=1):
        raise ValueError("future current_date")
    if current_date < today - timedelta(days=MAX_DATA_AGE_DAYS):
        raise ValueError("stale current_date")
    if ath_date > current_date:
        raise ValueError("ATH date after current date")

    if not isinstance(payload.get("total_records"), int) or payload["total_records"] < 1000:
        raise ValueError("history too short")

    fetched = datetime.fromisoformat(payload["fetched_at"].replace("Z", "+00:00"))
    if fetched.tzinfo is None:
        raise ValueError("fetched_at timezone missing")
    age = ((now or utcnow()) - fetched.astimezone(timezone.utc)).total_seconds()
    if age < -600:
        raise ValueError("fetched_at too far future")
    if max_fetch_age_seconds is not None and age > max_fetch_age_seconds:
        raise ValueError("fetched_at stale")
    return age


def write_atomic(payload):
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-latest", action="store_true")
    parser.add_argument("--max-fetch-age-seconds", type=int, default=7200)
    args = parser.parse_args(argv)
    try:
        if args.check_latest:
            payload = json.loads(OUT.read_text(encoding="utf-8"))
            age = validate_payload(payload, args.max_fetch_age_seconds)
            print(f"drawdown.json healthy; fetched_age_seconds={age:.0f}")
        else:
            rows, total = fetch_all()
            payload = build_payload(rows, total)
            validate_payload(payload, max_fetch_age_seconds=60)
            write_atomic(payload)
            print(json.dumps(payload))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
