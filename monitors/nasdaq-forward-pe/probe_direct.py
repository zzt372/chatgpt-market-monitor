#!/usr/bin/env python3
"""Read-only network probe: never writes production latest.json."""
import json, re, urllib.request, urllib.error
from datetime import datetime, timezone
from html import unescape
from urllib.parse import quote_plus

URLS = [
    "https://trendonify.com/united-states/stock-market/nasdaq-100/forward-pe-ratio",
    "https://api.trendonify.com/united-states/stock-market/nasdaq-100/forward-pe-ratio",
    "https://lite.duckduckgo.com/lite/?q=" + quote_plus('Trendonify "NASDAQ-100 Forward PE Ratio" percentile'),
]
output = {"probe_at": datetime.now(timezone.utc).isoformat(), "results": []}
try:
    from direct_source import parse_official_page
    from fetch import fetch_direct_once
    output["direct_parsed"] = list(parse_official_page(fetch_direct_once()))
except Exception as exc:
    output["direct_parse_error"] = type(exc).__name__ + ": " + str(exc)[:400]
try:
    with open("monitors/nasdaq-forward-pe/latest.json",encoding="utf-8") as f:
        output["dry_run_latest"] = json.load(f)
except Exception as exc:
    output["dry_run_latest_error"] = type(exc).__name__ + ": " + str(exc)[:200]
for url in URLS:
    item = {"host": url.split("/")[2]}
    try:
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0 (compatible; MonitorReliabilityTest/1.0)","Accept":"text/html,application/xhtml+xml;q=0.9,*/*;q=0.8","Accept-Language":"en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=20) as response:
            raw=response.read(600000)
            item["status"]=response.status
            item["content_type"]=response.headers.get("Content-Type")
            item["bytes"]=len(raw)
            s=unescape(re.sub(r"<[^>]*>"," ",raw.decode("utf-8","replace")))
            s=" ".join(s.split())
            item["contains_dedicated_identity"]="nasdaq-100" in s.lower() and "forward" in s.lower() and "p/e" in s.lower()
            item["contains_known_value"]="21.41" in s and "36.7" in s
            item["bot_challenge"]="select all squares containing a duck" in s.lower() or "unfortunately, bots use duckduckgo too" in s.lower()
            item["head_excerpt"]=s[:300]
    except urllib.error.HTTPError as exc:
        item["status"]=exc.code
        item["error"]=str(exc)[:200]
    except Exception as exc:
        item["error"]=type(exc).__name__ + ": " + str(exc)[:200]
    output["results"].append(item)
print(json.dumps(output,ensure_ascii=False,indent=2))
with open("monitors/nasdaq-forward-pe/probe-result.json","w",encoding="utf-8") as f:
    json.dump(output,f,ensure_ascii=False,indent=2)
    f.write("\n")
