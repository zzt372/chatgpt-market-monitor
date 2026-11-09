"""Index close history and trailing 252-session drawdown.

NDX and SOX come from the official Nasdaq API. Nasdaq does not carry the
S&P 500, so SPX comes from Cboe's public delayed-quote data (full daily close
history since 1975; it matched FRED's SP500 series on 2,506 of 2,513 days
checked on 2026-11-09, the remaining 7 being small differences from 2018-2020).
"""
import argparse
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

PAGE_SIZE = 5000
WINDOW = 252
# Alert decisions (policy v3) use the highest close of the trailing 3 years.
# The 252-session fields stay for reference and for older task instructions.
WINDOW_3Y = 756
# Publish an unchanged payload at most once per hour (fetched_at heartbeat);
# the alert task treats fetched_at older than 3 hours as stale.
HEARTBEAT_SECONDS = 3600
NEW_YORK = ZoneInfo("America/New_York")
QUOTE_DATE = re.compile(r"^\s*([A-Za-z]{3})\s+(\d{1,2}),\s+(\d{4})")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nasdaq.com/",
}
CBOE_HEADERS = dict(HEADERS, Referer="https://www.cboe.com/")
CBOE_API = "https://cdn-api.cboe.com/api/global/delayed_quotes"


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


def price(text):
    if not isinstance(text, str):
        raise ValueError("price must be string")
    value = float(text.replace(",", "").strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid price")
    return value


def finalized(day, local):
    """A session close is final once New York passes 16:15 on that day."""
    return day < local.date() or (day == local.date() and (local.hour, local.minute) >= (16, 15))


class IndexCollector:
    """NDX/SOX from the official Nasdaq API; subclasses swap the data source."""

    SYMBOLS = ("NDX", "SOX")
    SOURCE = "Nasdaq official API"
    SOURCE_KIND = "official-historical-close"
    FETCH_METHOD = "nasdaq-api"
    HISTORY_LABEL = "nasdaq-historical"
    QUOTE_LABEL = "nasdaq-quote-info"

    def __init__(self, symbol, out):
        if symbol not in self.SYMBOLS:
            raise ValueError("unsupported symbol")
        self.symbol = symbol
        self.out = Path(out)
        self.base = f"https://api.nasdaq.com/api/quote/{symbol}/historical"
        self.quote_url = f"https://api.nasdaq.com/api/quote/{symbol}/info?assetclass=index"

    def parse_quote(self, quote):
        """Normalize a Nasdaq /info quote: {day, close, previous, open} or None."""
        primary = quote.get("primaryData") if isinstance(quote, dict) else None
        if not isinstance(primary, dict):
            return None
        match = QUOTE_DATE.match(str(primary.get("lastTradeTimestamp") or ""))
        day = datetime.strptime(" ".join(match.groups()), "%b %d %Y").date() if match else None
        stats = quote.get("keyStats")
        prev = stats.get("previousclose") if isinstance(stats, dict) else None
        return {"day": day, "close": primary.get("lastSalePrice"),
                "previous": prev.get("value") if isinstance(prev, dict) else None,
                "open": str(quote.get("marketStatus", "")).lower() == "open"}

    def fetch_quote(self):
        req = Request(self.quote_url, headers=HEADERS)
        with urlopen(req, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"Nasdaq quote HTTP {response.status}")
            obj = json.load(response)
        if obj.get("status", {}).get("rCode") != 200:
            raise ValueError("Nasdaq quote rCode failure")
        data = obj.get("data")
        if not isinstance(data, dict) or data.get("symbol") != self.symbol:
            raise ValueError("quote symbol mismatch")
        return data

    def quote_supplement(self, quote, rows, now):
        """Return the latest close from the quote API when history lags behind it.

        The historical endpoint can publish a session's close many hours after
        the quote endpoint does (e.g. 2026-11-05 was still missing six hours
        after the close). The quote close is used only when it is a finalized
        weekday session newer than every finalized history row, and its
        previous close matches the newest history close, so no session is
        skipped. Returns (history-shaped row or None, reason).
        """
        local = now.astimezone(NEW_YORK)
        q = self.parse_quote(quote)
        if q is None:
            return None, "quote data missing"
        day = q["day"]
        if day is None:
            return None, "quote date unparseable"
        if day.weekday() >= 5 or day > local.date():
            return None, "quote date not a past weekday"
        if not finalized(day, local) or q["open"]:
            return None, "quote session not finalized"
        history = {}
        for row in rows:
            d = datetime.strptime(row["date"], "%m/%d/%Y").date()
            if finalized(d, local):
                history[d] = row["close"]
        if not history or day <= max(history):
            return None, "history already current"
        previous = price(q["previous"]) if q["previous"] is not None else None
        if previous is None or abs(previous - price(history[max(history)])) > 0.005:
            return None, "quote previous close does not match newest history close"
        close = q["close"]
        price(close)
        return {"date": day.strftime("%m/%d/%Y"), "close": close}, "quote close newer than history"

    def heartbeat_only(self, old, new, now):
        """True when only fetched_at moved and the published copy is still young."""
        if not isinstance(old, dict) or not isinstance(new, dict):
            return False
        strip = lambda p: {k: v for k, v in p.items() if k != "fetched_at"}
        if strip(old) != strip(new):
            return False
        try:
            age = self.validate_payload(old, now=now)
        except Exception:
            return False
        return age < HEARTBEAT_SECONDS

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

    def build_payload(self, rows, total_records, fetched_at=None, supplement=None):
        stamp = fetched_at or utcnow()
        if stamp.tzinfo is None:
            raise ValueError("fetched_at timezone missing")
        if type(total_records) is not int or len(rows) != total_records:
            raise ValueError("incomplete history/count mismatch")
        local = stamp.astimezone(NEW_YORK)
        parsed, dates = [], set()
        for row in list(rows) + ([supplement] if supplement else []):
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
        if len(parsed) < WINDOW_3Y:
            raise ValueError("history too short for 756 finalized sessions")
        current_date, current_close = parsed[0]
        ath_date, ath_close = max(parsed, key=lambda x: (x[1], x[0]))
        window = parsed[:WINDOW]
        high_date, high_close = max(window, key=lambda x: (x[1], x[0]))
        window3 = parsed[:WINDOW_3Y]
        high3_date, high3_close = max(window3, key=lambda x: (x[1], x[0]))
        from_quote = bool(supplement) and current_date.strftime("%m/%d/%Y") == supplement["date"]
        return {
            "schema_version": 2, "ok": True, "symbol": self.symbol,
            "current_close": current_close, "current_date": current_date.isoformat(),
            "ath_close": ath_close, "ath_date": ath_date.isoformat(),
            "drawdown_pct": round(drawdown(current_close, ath_close), 4),
            "window_sessions": WINDOW, "window_start_date": window[-1][0].isoformat(),
            "rolling_high_close": high_close, "rolling_high_date": high_date.isoformat(),
            "rolling_drawdown_pct": drawdown(current_close, high_close),
            "window_3y_sessions": WINDOW_3Y, "window_3y_start_date": window3[-1][0].isoformat(),
            "high_3y_close": high3_close, "high_3y_date": high3_date.isoformat(),
            "drawdown_3y_pct": drawdown(current_close, high3_close),
            "total_records": total_records, "finalized_records": len(parsed),
            "fetched_at": stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": self.SOURCE, "source_endpoint": self.base,
            "source_kind": self.SOURCE_KIND, "fetch_method": self.FETCH_METHOD,
            "supplemented_records": 1 if supplement else 0,
            "current_close_source": self.QUOTE_LABEL if from_quote else self.HISTORY_LABEL,
        }

    def validate_payload(self, payload, max_fetch_age_seconds=None, now=None):
        now = now or utcnow()
        if not isinstance(payload, dict) or payload.get("schema_version") != 2 or payload.get("ok") is not True:
            raise ValueError("schema/ok mismatch")
        for key, expected in {"symbol": self.symbol, "source": self.SOURCE,
                              "source_endpoint": self.base, "source_kind": self.SOURCE_KIND,
                              "fetch_method": self.FETCH_METHOD, "window_sessions": WINDOW}.items():
            if payload.get(key) != expected:
                raise ValueError(f"{key} mismatch")
        if payload.get("window_3y_sessions") != WINDOW_3Y:
            raise ValueError("window_3y_sessions mismatch")
        current, ath, high, high3 = (number(payload[k]) for k in
                                     ("current_close", "ath_close", "rolling_high_close", "high_3y_close"))
        if not 0 < current <= high <= high3 <= ath:
            raise ValueError("invalid close/ATH/3-year high/rolling high")
        for key, peak, tolerance in (("drawdown_pct", ath, 0.000051), ("rolling_drawdown_pct", high, 1e-9),
                                     ("drawdown_3y_pct", high3, 1e-9)):
            if abs(number(payload[key]) - (current / peak - 1) * 100) > tolerance:
                raise ValueError("drawdown mismatch")
        current_date, ath_date, high_date, start, high3_date, start3 = (
            datetime.strptime(payload[k], "%Y-%m-%d").date()
            for k in ("current_date", "ath_date", "rolling_high_date", "window_start_date",
                      "high_3y_date", "window_3y_start_date"))
        local = now.astimezone(NEW_YORK)
        if current_date > local.date() or (current_date == local.date() and (local.hour, local.minute) < (16, 15)):
            raise ValueError("unfinalized/future current_date")
        if current_date < local.date() - timedelta(days=7):
            raise ValueError("stale current_date")
        if not start <= high_date <= current_date or ath_date > current_date:
            raise ValueError("invalid history dates")
        if (current_date - start).days < WINDOW - 1:
            raise ValueError("invalid window dates")
        if (not start3 <= high3_date <= current_date or start3 > start
                or (current_date - start3).days < WINDOW_3Y - 1):
            raise ValueError("invalid 3-year window dates")
        for key in ("total_records", "finalized_records"):
            if type(payload.get(key)) is not int or payload[key] < WINDOW_3Y:
                raise ValueError("history too short")
        supplemented = payload.get("supplemented_records", 0)
        if supplemented not in (0, 1) or type(supplemented) is not int:
            raise ValueError("supplemented_records invalid")
        if payload.get("current_close_source", self.HISTORY_LABEL) not in (self.HISTORY_LABEL, self.QUOTE_LABEL):
            raise ValueError("current_close_source invalid")
        if payload["finalized_records"] > payload["total_records"] + supplemented:
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
                try:
                    quote = self.fetch_quote()
                except Exception as exc:
                    quote = None
                    print(f"WARN: quote fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                stamp = utcnow()
                supplement = None
                if quote is not None:
                    try:
                        supplement, reason = self.quote_supplement(quote, rows, stamp)
                        print(f"{self.symbol} quote check: {reason}", file=sys.stderr)
                    except Exception as exc:
                        # The quote endpoint only speeds things up; history stays authoritative.
                        print(f"WARN: quote check skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
                payload = self.build_payload(rows, total, fetched_at=stamp, supplement=supplement)
                self.validate_payload(payload, 60)
                try:
                    old = json.loads(self.out.read_text(encoding="utf-8"))
                except Exception:
                    old = None
                if self.heartbeat_only(old, payload, stamp):
                    print(f"{self.symbol} unchanged; keeping published copy until the hourly heartbeat")
                    return 0
                self.out.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.out.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                tmp.replace(self.out)
                print(json.dumps(payload))
            return 0
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2


class CboeIndexCollector(IndexCollector):
    """S&P 500 (SPX) from Cboe's public delayed-quote data; Nasdaq has no SPX."""

    SYMBOLS = ("SPX",)
    SOURCE = "Cboe delayed quotes"
    SOURCE_KIND = "exchange-historical-close"
    FETCH_METHOD = "cboe-cdn"
    HISTORY_LABEL = "cboe-historical"
    QUOTE_LABEL = "cboe-quote"

    def __init__(self, symbol, out):
        super().__init__(symbol, out)
        self.base = f"{CBOE_API}/charts/historical/_{symbol}.json"
        self.quote_url = f"{CBOE_API}/quotes/_{symbol}.json"

    def _get(self, url):
        req = Request(url, headers=CBOE_HEADERS)
        with urlopen(req, timeout=60) as response:
            if response.status != 200:
                raise RuntimeError(f"Cboe HTTP {response.status}")
            obj = json.load(response)
        if not isinstance(obj, dict) or obj.get("symbol") != f"_{self.symbol}":
            raise ValueError("symbol mismatch")
        return obj.get("data")

    def fetch_page(self, offset=0):
        raise NotImplementedError("Cboe returns the whole history in one document")

    def fetch_all(self):
        data = self._get(self.base)
        if not isinstance(data, list) or len(data) < WINDOW:
            raise ValueError("historical rows invalid")
        rows = []
        for row in data:
            if not isinstance(row, dict) or not isinstance(row.get("close"), str):
                raise ValueError("invalid history row")
            day = datetime.strptime(row["date"], "%Y-%m-%d")
            rows.append({"date": day.strftime("%m/%d/%Y"), "close": row["close"]})
        return rows, len(rows)

    def fetch_quote(self):
        data = self._get(self.quote_url)
        if not isinstance(data, dict):
            raise ValueError("quote data missing")
        return data

    def parse_quote(self, quote):
        """Normalize a Cboe quote. last_trade_time is New York time.

        The final SPX value carried a 16:14:59 timestamp (2026-11-06), so a
        last trade before 16:10 is treated as not final; early-close sessions
        are left to the history document.
        """
        if not isinstance(quote, dict):
            return None
        try:
            traded = datetime.strptime(str(quote.get("last_trade_time")), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            traded = None

        def text(value):
            return None if value is None else f"{number(value):.2f}"

        # prev_day_close rolls over overnight (on 2026-11-09 it already showed
        # the 11/06 close), so derive the prior session from the last change.
        current, change = quote.get("current_price"), quote.get("price_change")
        previous = None if current is None or change is None else number(current) - number(change)
        return {"day": traded.date() if traded else None,
                "close": text(current),
                "previous": text(previous),
                "open": traded is None or (traded.hour, traded.minute) < (16, 10)}
