"""Strict parser for the dedicated Trendonify Nasdaq-100 Forward P/E page.

Only extracts an explicit dated P/E plus explicit 10-year percentile
from the *same* official HTML document. No cross-page reconstruction.
"""
from __future__ import annotations

import html
import re
from datetime import datetime


class OfficialPageError(ValueError):
    pass


MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE = MONTH + r"\s+\d{1,2},\s+20\d{2}"


def visible_text(source: str) -> str:
    if not isinstance(source, str) or len(source) < 200:
        raise OfficialPageError("official HTML missing or too short")
    text = re.sub(r"<(?:script|style|noscript)\b[^>]*>[\s\S]*?</(?:script|style|noscript)\s*>", " ", source, flags=re.I)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def parse_official_page(source: str) -> tuple[float, float, str]:
    text = visible_text(source)
    if not re.search(r"NASDAQ[-\s]*100\s+Forward\s+P/?E\s+Ratio", text, re.I):
        raise OfficialPageError("dedicated Nasdaq-100 Forward P/E page identity absent")

    match_date = re.search(r"Last\s+Updated\s*:?\s*(" + DATE + r")", text, re.I)
    if not match_date:
        raise OfficialPageError("dedicated page Last Updated date absent")
    value_date = None
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            value_date = datetime.strptime(match_date.group(1), fmt).date().isoformat()
            break
        except ValueError:
            pass
    if not value_date:
        raise OfficialPageError("unparseable dedicated page date")

    match_pe = re.search(
        r"(?:the\s+)?NASDAQ[-\s]*100\s+currently\s+trades\s+at\s+a\s+forward\s+P/?E\s+ratio\s+of\s+(\d+(?:\.\d+)?)",
        text, re.I
    )
    if not match_pe:
        raise OfficialPageError("dated official forward P/E statement absent")
    pe = float(match_pe.group(1))

    match_10y = re.search(
        r"Looking\s+at\s+the\s+longer\s+10[-\s]*year\s+period(?:(?!Looking\s+at\s+the\s+longer).){0,450}?"
        r"current\s+reading\s+ranks\s+in\s+the\s+(\d+(?:\.\d+)?)(?:st|nd|rd|th)?\s+percentile",
        text, re.I
    )
    if not match_10y:
        raise OfficialPageError("explicit 10-year percentile absent")
    pct = float(match_10y.group(1))

    # The matching as-of date must agree with Last Updated. Prevent mixing
    # a stale headline with unrelated narrative data.
    match_asof = re.search(
        r"NASDAQ[-\s]*100\s+currently\s+trades\s+at\s+a\s+forward\s+P/?E\s+ratio\s+of\s+"
        r"\d+(?:\.\d+)?\s+as\s+of\s+(" + DATE + r")", text, re.I
    )
    if match_asof and match_asof.group(1).casefold() != match_date.group(1).casefold():
        raise OfficialPageError("dedicated page Last Updated/as-of dates disagree")
    return pe, pct, value_date
