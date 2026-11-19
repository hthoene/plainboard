"""The plan limits of every provider account as last reported. Reading them costs no tokens, or
for a Claude subscription signed in with a token a handful (the runner asks with a minimal turn).

Stored per account key. Window names: Claude's as it reports them (five_hour, seven_day, seven_day_opus ...),
Codex's prefixed with codex_.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from statistics import median

from plainboard_protocol import LimitWindow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, Setting
from ..schemas import AccountLimits, LimitForecast, LimitsOut

KEY = "provider_limits"
DAY = 86400
WEEKLY_MARK = "seven_day"
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


async def _stored(session: AsyncSession) -> tuple[Setting, dict]:
    row = await session.get(Setting, KEY) or Setting(key=KEY, secret=False)
    return row, (json.loads(row.value) if row.value else {}).get("accounts", {})


FRESH_SECONDS = 600  # a report this new needs no asking: an account in use reports with every turn


async def fresh(session: AsyncSession) -> set[str]:
    """Accounts (by key) whose limits were reported a moment ago."""
    _row, accounts = await _stored(session)
    now = time.time()
    return {key for key, stored in accounts.items() if now - (stored.get("updated_at") or 0) < FRESH_SECONDS}


async def record(session: AsyncSession, account_key: str, windows: list[LimitWindow], complete: bool = False) -> None:
    """Store a report of an account's limits. ``complete``: the report lists every window the
    account has, so one it leaves out is gone (a plan that dropped its 5-hour limit) and is
    forgotten, instead of being shown as an empty window for ever."""
    row, accounts = await _stored(session)
    stored = accounts.setdefault(account_key, {})
    known, growth = stored.get("windows", {}), stored.get("growth", {})
    if complete:
        reported = {w.name for w in windows}
        known = {name: w for name, w in known.items() if name in reported}
        growth = {name: g for name, g in growth.items() if name in reported}
    now = time.time()
    for window in windows:
        if WEEKLY_MARK in window.name:
            track_growth(growth, window, known.get(window.name), now)
    for stored_window in known.values():  # stored before windows knew when they were last reported
        stored_window.setdefault("seen_at", stored.get("updated_at") or 0)
    known.update({w.name: {**w.model_dump(), "seen_at": int(now)} for w in windows})
    accounts[account_key] = {"windows": known, "growth": growth, "updated_at": int(now)}
    row.value = json.dumps({"accounts": accounts})
    session.add(row)
    await session.commit()


# The plans of the accounts, by key: {"name", "size", "checked_at"}. A setting of its own, so that
# storing one never races with a turn's limit report.
PLANS_KEY = "provider_plans"
PLAN_CHECK_SECONDS = 6 * 3600


async def _plans(session: AsyncSession) -> tuple[Setting, dict]:
    row = await session.get(Setting, PLANS_KEY) or Setting(key=PLANS_KEY, secret=False)
    return row, json.loads(row.value) if row.value else {}


async def record_plan(session: AsyncSession, account_key: str, name: str | None, size: float | None) -> None:
    """Store what the provider said about the account's plan; nothing known keeps what was known."""
    row, plans = await _plans(session)
    known = plans.get(account_key) or {}
    plans[account_key] = {"name": name or known.get("name"), "size": size or known.get("size"), "checked_at": int(time.time())}
    row.value = json.dumps(plans)
    session.add(row)
    await session.commit()


async def plan_checked(session: AsyncSession) -> set[str]:
    """Accounts (by key) whose plan was asked for a moment ago."""
    _row, plans = await _plans(session)
    now = time.time()
    return {key for key, plan in plans.items() if now - (plan.get("checked_at") or 0) < PLAN_CHECK_SECONDS}


async def plan_sizes(session: AsyncSession) -> dict[str, float]:
    """The size of each account's plan (by key) as the provider said it, where it did."""
    _row, plans = await _plans(session)
    return {key: plan["size"] for key, plan in plans.items() if plan.get("size")}


UNREPORTED_SECONDS = 3 * 3600


def _windows(stored: dict, now: float) -> list[LimitWindow]:
    """The account's windows as they are now. A window that the account's reports have left out for
    hours is not shown and does not count: its number is old, and nothing would ever correct it.
    A provider reports some windows only with requests that touch them (a limit for certain
    models), and a plan can be reset before a window's own reset time. An account that is avoided
    because of such a window would never get the request that reports it afresh. If the limit
    still holds, the first request that touches it brings it back."""
    kept = list(stored.get("windows", {}).values())
    newest = max((w.get("seen_at") or 0 for w in kept), default=0)
    windows = [LimitWindow(**{k: v for k, v in w.items() if k != "seen_at"}) for w in kept if newest - (w.get("seen_at") or newest) < UNREPORTED_SECONDS]
    for window in windows:
        if window.resets_at and window.resets_at <= now:  # it has reset since the last report
            window.utilization, window.resets_at, window.status = 0.0, None, None
    return windows


async def usage(session: AsyncSession) -> dict[str, float]:
    """How full each account is: its fullest window, 1 if the provider turned it away."""
    _, accounts = await _stored(session)
    now = time.time()
    used = {}
    for key, stored in accounts.items():
        windows = _windows(stored, now)
        used[key] = max([1.0 if w.status == "rejected" else w.utilization for w in windows], default=0.0)
    return used


async def current(session: AsyncSession) -> LimitsOut:
    _, stored = await _stored(session)
    _, plans = await _plans(session)
    accounts = (await session.scalars(select(Account).where(Account.enabled.is_(True)).order_by(Account.provider, Account.id))).all()  # as accounts.list_accounts
    now = time.time()
    out = []
    for account in accounts:
        # An API key has no plan limits, so what a subscription reported earlier no longer applies.
        entry = AccountLimits(account_id=account.id, provider=account.provider, label=account.label, available=account.auth_mode != "api_key")
        data = stored.get(account.key, {})
        if entry.available:
            entry.plan, entry.plan_size = (plans.get(account.key) or {}).get("name"), (plans.get(account.key) or {}).get("size")
        if entry.available and (windows := _windows(data, now)):
            growth = data.get("growth", {})
            forecasts = [forecast(w, growth.get(w.name, {}).get("days", {}), now) for w in windows if WEEKLY_MARK in w.name]
            entry.windows, entry.forecasts, entry.updated_at = windows, [f for f in forecasts if f], data.get("updated_at")
        out.append(entry)
    return LimitsOut(accounts=out)
