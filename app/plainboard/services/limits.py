"""The account's plan limits as last reported by a run. Reading them costs no tokens."""

from __future__ import annotations

import json
import time

from plainboard_protocol import LimitWindow
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Setting
from ..schemas import LimitsOut
from . import settings as settings_service

KEY = "provider_limits"


async def record(session: AsyncSession, windows: list[LimitWindow]) -> None:
    row = await session.get(Setting, KEY) or Setting(key=KEY, secret=False)
    known = json.loads(row.value)["windows"] if row.value else {}
    known.update({w.name: w.model_dump() for w in windows})
    row.value = json.dumps({"windows": known, "updated_at": int(time.time())})
    session.add(row)
    await session.commit()


async def current(session: AsyncSession) -> LimitsOut:
    # An API key has no plan limits, so what a subscription reported earlier no longer applies.
    if settings_service.effective_auth_mode(await settings_service.get_all(session)) == "api_key":
        return LimitsOut()
    row = await session.get(Setting, KEY)
    if not row or not row.value:
        return LimitsOut()
    stored = json.loads(row.value)
    windows = [LimitWindow(**w) for w in stored["windows"].values()]
    now = time.time()
    for window in windows:
        if window.resets_at and window.resets_at <= now:  # it has reset since the last run
            window.utilization, window.resets_at, window.status = 0.0, None, None
    return LimitsOut(windows=windows, updated_at=stored["updated_at"])
