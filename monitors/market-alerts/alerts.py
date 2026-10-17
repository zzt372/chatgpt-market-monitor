"""Pure per-index drawdown notification decisions; no delivery or file writes."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import math


POLICY_VERSION = 1
STATE_VERSION = 1
THRESHOLDS_PCT = {
    "NDX": (-10, -15, -20, -25),
    "SOX": (-20, -25, -30, -35),
}
RESET_DRAWDOWN_PCT = -5
WINDOW_SESSIONS = 252
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


def _validate_observation(payload, now):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if payload.get("schema_version") != 2 or payload.get("ok") is not True:
        raise ValueError("unsupported or unhealthy payload")
    symbol = payload.get("symbol")
    if symbol not in THRESHOLDS_PCT:
        raise ValueError("unsupported symbol")
    if payload.get("window_sessions") != WINDOW_SESSIONS:
        raise ValueError("window_sessions must be 252")
    current = _number(payload.get("current_close"), "current_close")
    high = _number(payload.get("rolling_high_close"), "rolling_high_close")
    drawdown = _number(payload.get("rolling_drawdown_pct"), "rolling_drawdown_pct")
    if current <= 0 or high <= 0 or current > high:
        raise ValueError("invalid current or rolling high close")
    expected = (current / high - 1) * 100
    if not -100 < drawdown <= 0 or abs(drawdown - expected) > 0.0001:
        raise ValueError("rolling drawdown does not match closes")
    current_date = _date(payload.get("current_date"), "current_date")
    high_date = _date(payload.get("rolling_high_date"), "rolling_high_date")
    if current_date > now.date():
        raise ValueError("current_date is in the future")
    if current_date < now.date() - timedelta(days=MAX_DATA_AGE_DAYS):
        raise ValueError("current_date is stale")
    if high_date > current_date:
        raise ValueError("rolling high date is after current_date")
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
    precise_drawdown = float((Decimal(str(current)) / Decimal(str(high)) - 1) * 100)
    return symbol, current_date, precise_drawdown


def _validate_state(state, symbol):
    if not isinstance(state, dict):
        raise ValueError("state must be an object or None for first observation")
    if state.get("schema_version") != STATE_VERSION:
        raise ValueError("unsupported state schema")
    if state.get("policy_version") != POLICY_VERSION:
        raise ValueError("state policy version requires explicit migration")
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
    return _date(state.get("last_observation_date"), "last_observation_date")


def evaluate_observation(payload, state=None, *, now=None):
    """Return a new state and at most one alert, without mutating either input.

    Call separately for NDX and SOX, and persist the returned states independently.
    A missing state explicitly means first observation: all already reached levels
    become the baseline without delivery. Never substitute None for an unreadable
    or corrupted existing state. Invalid/stale data raises ValueError before any
    decision. Fear & Greed is optional context and is not an entry requirement.
    """
    stamp = now or datetime.now(timezone.utc)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    stamp = stamp.astimezone(timezone.utc)
    symbol, current_date, drawdown = _validate_observation(payload, stamp)
    reached = [level for level in THRESHOLDS_PCT[symbol] if drawdown <= level]
    if state is None:
        initialized = {
            "schema_version": STATE_VERSION,
            "policy_version": POLICY_VERSION,
            "symbol": symbol,
            "episode_id": 1 if reached else 0,
            "episode_active": bool(reached),
            "notified_thresholds_pct": reached,
            "last_observation_date": current_date.isoformat(),
            "last_drawdown_pct": drawdown,
        }
        return {"ok": True, "status": "baseline", "state": initialized,
                "notification": None}

    previous_date = _validate_state(state, symbol)
    updated = deepcopy(state)
    if current_date <= previous_date:
        return {"ok": True, "status": "ignored", "state": updated,
                "notification": None}

    updated["last_observation_date"] = current_date.isoformat()
    updated["last_drawdown_pct"] = drawdown
    if drawdown >= RESET_DRAWDOWN_PCT:
        was_active = updated["episode_active"]
        updated["episode_active"] = False
        updated["notified_thresholds_pct"] = []
        return {"ok": True, "status": "reset" if was_active else "unchanged",
                "state": updated, "notification": None}

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
    }
    return {"ok": True, "status": "alert", "state": updated,
            "notification": notification}
