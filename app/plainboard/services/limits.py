"""The plan limits of the signed-in subscriptions as last reported. Reading them costs no tokens.

Window names: Claude's as it reports them (seven_day, seven_day_opus ...), Codex's prefixed with codex_.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from statistics import median

from plainboard_protocol import LimitWindow
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting
from ..schemas import LimitForecast, LimitsOut
from . import settings as settings_service

KEY = "provider_limits"
DAY = 86400
WEEKLY_MARK = "seven_day"
CODEX_PREFIX = "codex_"
RETIRED = {"five_hour"}  # Claude dropped its 5-hour limit; a window stored from before is not shown
WEEK_DAYS = 7
MIN_HISTORY_DAYS = 3
TIGHT_ABOVE = 0.9


def _day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).date().isoformat()


def track_growth(growth: dict, window: LimitWindow, previous: dict | None, now: float) -> None:
    """Add what a weekly window grew by to today's total. Only a new peak counts, because reports
    from different sources round differently."""
    entry = growth.setdefault(window.name, {"peak": window.utilization, "days": {}})
    if previous and previous.get("resets_at") and previous["resets_at"] <= now:
        entry["peak"] = 0.0  # a new week has started
    today = _day(now)
    entry["days"][today] = entry["days"].get(today, 0.0) + max(0.0, window.utilization - entry["peak"])
    entry["peak"] = max(entry["peak"], window.utilization)
    oldest = _day(now - WEEK_DAYS * DAY)
    entry["days"] = {day: used for day, used in entry["days"].items() if day >= oldest}


def daily_rate(days: dict[str, float], now: float) -> tuple[float, int] | None:
    """Typical use per day and the number of complete days it is based on. A plain median is 0 for
    someone who works three days a week, so it is the median of the days with use, scaled by how
    often such days occur."""
    if not days:
        return None
    first = min(days)
    complete = [_day(now - n * DAY) for n in range(1, WEEK_DAYS + 1)]
    tracked = [days.get(day, 0.0) for day in complete if day >= first]
    if len(tracked) < MIN_HISTORY_DAYS:
        return None
    used = [value for value in tracked if value > 0]
    return (median(used) * len(used) / len(tracked) if used else 0.0), len(tracked)


def forecast(window: LimitWindow, days: dict[str, float], now: float) -> LimitForecast | None:
    """Where a weekly window ends up at this pace. Without enough history: the pace of this week so far."""
    if not window.resets_at or window.resets_at <= now:
        return None
    days_left = (window.resets_at - now) / DAY
    estimate = daily_rate(days, now)
    if estimate:
        rate, basis_days = estimate
    else:
        elapsed = WEEK_DAYS - days_left
        if elapsed < 1:
            return None
        rate, basis_days = window.utilization / elapsed, 0
    projected = window.utilization + rate * days_left
    over = projected > 1 or window.utilization >= 1
    return LimitForecast(
        name=window.name,
        status="over" if over else "tight" if projected > TIGHT_ABOVE else "ok",
        daily=rate,
        budget=max(0.0, 1 - window.utilization) / days_left,
        projected=projected,
        runs_out_at=int(now + (1 - window.utilization) / rate * DAY) if over and window.utilization < 1 else None,
        basis_days=basis_days,
    )


async def record(session: AsyncSession, windows: list[LimitWindow]) -> None:
    row = await session.get(Setting, KEY) or Setting(key=KEY, secret=False)
    stored = json.loads(row.value) if row.value else {}
    known, growth = stored.get("windows", {}), stored.get("growth", {})
    now = time.time()
    for window in windows:
        if WEEKLY_MARK in window.name:
            track_growth(growth, window, known.get(window.name), now)
    known.update({w.name: w.model_dump() for w in windows})
    for name in RETIRED:
        known.pop(name, None)
    row.value = json.dumps({"windows": known, "growth": growth, "updated_at": int(now)})
    session.add(row)
    await session.commit()


async def current(session: AsyncSession) -> LimitsOut:
    # An API key has no plan limits, so what a subscription reported earlier no longer applies.
    values = await settings_service.get_all(session)
    claude_key = settings_service.effective_auth_mode(values) == "api_key"
    codex_key = values.get("codex_auth_mode") == "api_key"
    row = await session.get(Setting, KEY)
    stored = json.loads(row.value) if row and row.value else {"windows": {}}
    windows = [
        LimitWindow(**w)
        for w in stored["windows"].values()
        if w["name"] not in RETIRED and not (codex_key if w["name"].startswith(CODEX_PREFIX) else claude_key)
    ]
    if not windows:
        return LimitsOut(available=not claude_key)
    now = time.time()
    for window in windows:
        if window.resets_at and window.resets_at <= now:  # it has reset since the last run
            window.utilization, window.resets_at, window.status = 0.0, None, None
    growth = stored.get("growth", {})
    forecasts = [forecast(w, growth.get(w.name, {}).get("days", {}), now) for w in windows if WEEKLY_MARK in w.name]
    return LimitsOut(windows=windows, forecasts=[f for f in forecasts if f], updated_at=stored["updated_at"])
