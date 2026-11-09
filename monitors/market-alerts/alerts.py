"""Pure per-index drawdown notification decisions; no delivery or file writes.

Policy v3 (2026-11-10) decides on the drawdown from the highest close of the
trailing 3 years (756 sessions, `drawdown_3y_pct`). Replaying this state machine
over NASDAQ100, S&P 500, SOX, the Nasdaq Composite (1971-), the Nikkei 225
(1949-) and Shiller's monthly US stocks (1871-) showed why:

* The all-time high stayed silent through every crash that came while an old
  peak was still unrecovered: NASDAQ100/SOX in 2008, the Nikkei for 32 years
  (1992-2024), US stocks in 1937-38. The 3-year high alerted all of them.
* The trailing 252 sessions under-measured multi-year declines
  (NASDAQ100 2000-02: -67.4% instead of -82.9%).
* 4- and 5-year windows missed crashes the 3-year window caught
  (S&P 500 2011, SOX 2006 and 2011, the Nikkei 1997-2001).
"""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math


POLICY_VERSION = 3
STATE_VERSION = 3
THRESHOLDS_PCT = {
    "NDX": (-20, -30, -40, -50),
    "SPX": (-15, -20, -30, -40),
    "SOX": (-30, -40, -50, -60),
    "N225": (-20, -30, -40, -50),
}
# 3-year-high drawdown at the trough of past major crashes, for scale in the
# notification text only; never a decision input.
REFERENCE_BOTTOMS_PCT = {
    "NDX": (("2000-02", -82.9), ("2008", -53.7), ("2020", -28.0), ("2022", -35.6)),
    "SPX": (("2000-02", -49.1), ("2008-09", -56.8), ("2020", -33.9), ("2022", -25.4)),
    "SOX": (("2000-02", -83.9), ("2008", -68.9), ("2020", -35.0), ("2022", -46.5)),
    "N225": (("1990-92", -63.2), ("2000-03", -63.5), ("2008-09", -61.4), ("2020", -31.8), ("2024", -25.5)),
}
RESET_DRAWDOWN_PCT = -5
WINDOW_RECOVERY_CONFIRMATION_PCT = 5
WINDOW_SESSIONS = 756
MAX_FETCH_AGE_SECONDS = 10800
MAX_DATA_AGE_DAYS = 7


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _date(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        result = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date") from error
    if result.isoformat() != value:
        raise ValueError(f"{name} must be an ISO date")
    return result


def _drawdown(close, high):
    return float((Decimal(str(close)) / Decimal(str(high)) - 1) * 100)


def _recovery_target(close):
    multiplier = Decimal(1) + Decimal(WINDOW_RECOVERY_CONFIRMATION_PCT) / 100
    return float(Decimal(str(close)) * multiplier)


def _price_recovered(close, high):
    multiplier = Decimal(1) + Decimal(RESET_DRAWDOWN_PCT) / 100
    return Decimal(str(close)) >= Decimal(str(high)) * multiplier


def _validate_observation(payload, now):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if payload.get("schema_version") != 2 or payload.get("ok") is not True:
        raise ValueError("unsupported or unhealthy payload")
    symbol = payload.get("symbol")
    if symbol not in THRESHOLDS_PCT:
        raise ValueError("unsupported symbol")
    if payload.get("window_3y_sessions") != WINDOW_SESSIONS:
        raise ValueError("window_3y_sessions must be 756")
    current = _number(payload.get("current_close"), "current_close")
    high = _number(payload.get("high_3y_close"), "high_3y_close")
    drawdown = _number(payload.get("drawdown_3y_pct"), "drawdown_3y_pct")
    if current <= 0 or high <= 0 or current > high:
        raise ValueError("invalid current or 3-year high close")
    expected = (current / high - 1) * 100
    if not -100 < drawdown <= 0 or abs(drawdown - expected) > 0.0001:
        raise ValueError("3-year drawdown does not match closes")
    current_date = _date(payload.get("current_date"), "current_date")
    high_date = _date(payload.get("high_3y_date"), "high_3y_date")
    ath = _number(payload.get("ath_close"), "ath_close")
    ath_date = _date(payload.get("ath_date"), "ath_date")
    ath_drawdown = _number(payload.get("drawdown_pct"), "drawdown_pct")
    if ath < high or ath_date > current_date:
        raise ValueError("invalid ATH close/date")
    if abs(ath_drawdown - _drawdown(current, ath)) > 0.000051:
        raise ValueError("ATH drawdown does not match closes")
    if current_date > now.date():
        raise ValueError("current_date is in the future")
    if current_date < now.date() - timedelta(days=MAX_DATA_AGE_DAYS):
        raise ValueError("current_date is stale")
    if high_date > current_date:
        raise ValueError("3-year high date is after current_date")
    raw_stamp = payload.get("fetched_at")
    if not isinstance(raw_stamp, str):
        raise ValueError("fetched_at must include a timezone")
    try:
        fetched = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid fetched_at") from error
    if fetched.tzinfo is None:
        raise ValueError("fetched_at must include a timezone")
    age = (now - fetched.astimezone(timezone.utc)).total_seconds()
    if age < -600:
        raise ValueError("fetched_at is in the future")
    if age > MAX_FETCH_AGE_SECONDS:
        raise ValueError("fetched_at is stale")
    precise_drawdown = _drawdown(current, high)
    context = {
        "current_close": current,
        "high_3y_close": high,
        "high_3y_date": high_date.isoformat(),
        "drawdown_3y_pct": precise_drawdown,
        "ath_close": ath,
        "ath_date": ath_date.isoformat(),
        "ath_drawdown_pct": _drawdown(current, ath),
        "reference_bottoms_pct": [list(item) for item in REFERENCE_BOTTOMS_PCT[symbol]],
    }
    return symbol, current_date, precise_drawdown, context


def _validate_state(state, symbol):
    if not isinstance(state, dict):
        raise ValueError("state must be an object or None for first observation")
    if state.get("schema_version") != STATE_VERSION or state.get("policy_version") != POLICY_VERSION:
        raise ValueError("unsupported state schema or policy version")
    if state.get("symbol") != symbol:
        raise ValueError("state symbol mismatch")
    episode = state.get("episode_id")
    if isinstance(episode, bool) or not isinstance(episode, int) or episode < 0:
        raise ValueError("invalid episode_id")
    active = state.get("episode_active")
    if not isinstance(active, bool):
        raise ValueError("invalid episode_active")
    notified = state.get("notified_thresholds_pct")
    thresholds = list(THRESHOLDS_PCT[symbol])
    if not isinstance(notified, list) or notified != thresholds[:len(notified)]:
        raise ValueError("invalid notified thresholds")
    if bool(notified) != active or (active and episode < 1):
        raise ValueError("inconsistent episode state")
    _number(state.get("last_drawdown_pct"), "last_drawdown_pct")
    previous_high = _number(state.get("last_high_3y_close"), "last_high_3y_close")
    if previous_high <= 0:
        raise ValueError("invalid last 3-year high")
    target = state.get("recovery_target_close")
    if target is not None and (_number(target, "recovery_target_close") <= 0 or not active):
        raise ValueError("invalid recovery target")
    return _date(state.get("last_observation_date"), "last_observation_date")


def _baseline(symbol, current_date, drawdown, high, reached):
    """All already-reached levels become history without any delivery."""
    return {
        "schema_version": STATE_VERSION,
        "policy_version": POLICY_VERSION,
        "symbol": symbol,
        "episode_id": 1 if reached else 0,
        "episode_active": bool(reached),
        "notified_thresholds_pct": reached,
        "last_observation_date": current_date.isoformat(),
        "last_drawdown_pct": drawdown,
        "last_high_3y_close": high,
        "recovery_target_close": None,
    }


def evaluate_observation(payload, state=None, *, now=None):
    """Return a new state and at most one alert, without mutating either input.

    Call separately for each index (NDX, SPX, SOX, N225) and persist the returned
    states independently.
    A missing state explicitly means first observation: all already reached levels
    become the baseline without delivery. A v1/v2 state (other reference high and
    other levels) is re-baselined the same way, once, for its own symbol. Never
    substitute None for an unreadable or corrupted v3 state. Invalid/stale data
    raises ValueError before any decision. Fear & Greed is optional context and is
    not an entry requirement.
    """
    stamp = now or datetime.now(timezone.utc)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    stamp = stamp.astimezone(timezone.utc)
    symbol, current_date, drawdown, context = _validate_observation(payload, stamp)
    current = context["current_close"]
    high = context["high_3y_close"]
    reached = [level for level in THRESHOLDS_PCT[symbol] if drawdown <= level]
    if state is None:
        return {"ok": True, "status": "baseline",
                "state": _baseline(symbol, current_date, drawdown, high, reached),
                "notification": None}
    if isinstance(state, dict) and state.get("schema_version") in (1, 2):
        # Earlier policies measured from another high with other levels, so their
        # notification history cannot be mapped onto v3 levels.
        if state.get("symbol") != symbol:
            raise ValueError("state symbol mismatch")
        return {"ok": True, "status": "migrated",
                "state": _baseline(symbol, current_date, drawdown, high, reached),
                "notification": None}

    previous_date = _validate_state(state, symbol)
    updated = deepcopy(state)
    if current_date <= previous_date:
        return {"ok": True, "status": "ignored", "state": updated,
                "notification": None}

    previous_high = updated["last_high_3y_close"]
    updated["last_observation_date"] = current_date.isoformat()
    updated["last_drawdown_pct"] = drawdown
    updated["last_high_3y_close"] = high
    if drawdown >= RESET_DRAWDOWN_PCT and updated["episode_active"]:
        target = updated["recovery_target_close"]
        if target is not None:
            status = "reset" if Decimal(str(current)) >= Decimal(str(target)) else "recovery_pending"
        elif _price_recovered(current, previous_high):
            status = "reset"
        else:
            # The 3-year high fell out of the window. A flat price cannot rearm.
            updated["recovery_target_close"] = _recovery_target(current)
            status = "recovery_pending"
        if status == "reset":
            updated["episode_active"] = False
            updated["notified_thresholds_pct"] = []
            updated["recovery_target_close"] = None
        return {"ok": True, "status": status, "state": updated,
                "notification": None}

    if drawdown < RESET_DRAWDOWN_PCT:
        # A fresh decline cancels only the pending recovery check, not history.
        updated["recovery_target_close"] = None

    newly_reached = [level for level in reached
                     if level not in updated["notified_thresholds_pct"]]
    if not newly_reached:
        return {"ok": True, "status": "unchanged", "state": updated,
                "notification": None}
    if not updated["episode_active"]:
        updated["episode_id"] += 1
        updated["episode_active"] = True
    updated["notified_thresholds_pct"] = [
        level for level in THRESHOLDS_PCT[symbol]
        if level in reached or level in updated["notified_thresholds_pct"]
    ]
    notification = {
        "symbol": symbol,
        "episode_id": updated["episode_id"],
        "current_date": current_date.isoformat(),
        "drawdown_pct": drawdown,
        "threshold_pct": min(newly_reached),
        "newly_crossed_thresholds_pct": newly_reached,
        **context,
    }
    return {"ok": True, "status": "alert", "state": updated,
            "notification": notification}
