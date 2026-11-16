#!/usr/bin/env python3
"""Fetch CNN Fear & Greed Index from CNN's official JSON endpoint.

Writes latest.json only after a fully validated successful fetch.
No third-party index or proxy is used.
"""

from __future__ import annotations

import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
OFFICIAL_PAGE = "https://www.cnn.com/markets/fear-and-greed"
OUTPUT = Path("latest.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.cnn.com",
    "Referer": OFFICIAL_PAGE,
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def category_from_score(score: float) -> str:
    if 0 <= score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score <= 55:
        return "Neutral"
    if score <= 75:
        return "Greed"
    if score <= 100:
        return "Extreme Greed"
    raise ValueError(f"score out of range: {score}")


def normalize_rating(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("_", " ").replace("-", " ")
    text = " ".join(text.split()).lower()
    mapping = {
        "extreme fear": "Extreme Fear",
        "fear": "Fear",
        "neutral": "Neutral",
        "greed": "Greed",
        "extreme greed": "Extreme Greed",
    }
    return mapping.get(text, str(value).strip())


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 10_000_000_000:  # milliseconds
            seconds /= 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc)

    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp is missing")

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_block(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("JSON root is not an object")

    block = payload.get("fear_and_greed")
    if isinstance(block, dict):
        return block

    headline = payload.get("headline")
    if isinstance(headline, dict):
        block = headline.get("fear_and_greed")
        if isinstance(block, dict):
            return block

    raise ValueError("fear_and_greed block not found")


def validate_payload(payload: Any) -> dict[str, Any]:
    block = extract_block(payload)

    score_raw = block.get("score")
    if isinstance(score_raw, bool):
        raise ValueError("score is not numeric")
    try:
        score = float(score_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("score is missing or not numeric") from exc

    if not math.isfinite(score) or not 0 <= score <= 100:
        raise ValueError(f"invalid score: {score_raw!r}")

    timestamp = parse_timestamp(block.get("timestamp"))
    now = datetime.now(timezone.utc)
    age = now - timestamp

    # Weekend/US holiday carry-over is normal. Reject only clearly stale data.
    if age > timedelta(days=6):
        raise ValueError(f"timestamp is stale: {timestamp.isoformat()}")
    if age < timedelta(hours=-2):
        raise ValueError(f"timestamp is unexpectedly in the future: {timestamp.isoformat()}")

    category = category_from_score(score)
    rating = normalize_rating(block.get("rating")) or category

    return {
        "score": score,
        "rating": rating,
        "category": category,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
    }


def request_json(url: str, attempts: int = 3, timeout: int = 20) -> Any:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            req = Request(url, headers=HEADERS, method="GET")
            with urlopen(req, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
                if status != 200:
                    raise RuntimeError(f"HTTP {status}")
                if not body.strip():
                    raise RuntimeError("empty response body")
                return json.loads(body.decode("utf-8-sig"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, RuntimeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(f"failed after {attempts} attempts: {last_error}")


def fetch_latest() -> dict[str, Any]:
    ny_today = datetime.now(ZoneInfo("America/New_York")).date()
    dated_url = f"{ROOT_URL}/{(ny_today - timedelta(days=7)).isoformat()}"

    routes = [
        ("root JSON", ROOT_URL),
        ("dated JSON", dated_url),
    ]
    failures: list[str] = []

    for route_name, url in routes:
        try:
            payload = request_json(url)
            data = validate_payload(payload)
            data.update(
                {
                    "source": "CNN official",
                    "source_url": url,
                    "endpoint": route_name,
                    "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "ok": True,
                }
            )
            return data
        except Exception as exc:  # keep fallback path alive and report all failures
            failures.append(f"{route_name}: {type(exc).__name__}: {exc}")

    raise RuntimeError(" | ".join(failures))


def main() -> int:
    try:
        data = fetch_latest()
        temp = OUTPUT.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(OUTPUT)
        print(json.dumps(data, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Do not touch latest.json on failure; preserve the last known-good value.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
