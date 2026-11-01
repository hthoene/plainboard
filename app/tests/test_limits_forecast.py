from datetime import UTC, datetime

from plainboard_protocol import LimitWindow

from plainboard.services.limits import DAY, daily_rate, forecast, track_growth

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC).timestamp()


def history(*values: float) -> dict[str, float]:
    """Use per day, most recent complete day first; today has none yet."""
    return {datetime.fromtimestamp(NOW - n * DAY, UTC).date().isoformat(): v for n, v in enumerate(values, 1)} | {datetime.fromtimestamp(NOW, UTC).date().isoformat(): 0.0}


def weekly(used: float, days_left: float) -> LimitWindow:
    return LimitWindow(name="seven_day", utilization=used, resets_at=int(NOW + days_left * DAY))


def test_growth_counts_new_peaks_only_and_restarts_with_a_new_week():
    growth: dict = {}
    track_growth(growth, weekly(0.40, 3), None, NOW)
    track_growth(growth, weekly(0.45, 3), weekly(0.40, 3).model_dump(), NOW)
    track_growth(growth, weekly(0.44, 3), weekly(0.45, 3).model_dump(), NOW)  # other rounding
    track_growth(growth, weekly(0.46, 3), weekly(0.44, 3).model_dump(), NOW)
    today = datetime.fromtimestamp(NOW, UTC).date().isoformat()
    assert round(growth["seven_day"]["days"][today], 2) == 0.06  # what was used before tracking is unknown
    expired = LimitWindow(name="seven_day", utilization=0.9, resets_at=int(NOW - 10)).model_dump()
    track_growth(growth, weekly(0.03, 7), expired, NOW)
    assert round(growth["seven_day"]["days"][today], 2) == 0.09


def test_rate_is_robust_and_counts_idle_days():
    assert daily_rate(history(0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.7), NOW) == (0.1, 7)  # one outlier
    rate, days = daily_rate(history(0.3, 0, 0.3, 0, 0.3, 0, 0), NOW)
    assert days == 7 and round(rate, 3) == round(0.3 * 3 / 7, 3)  # a plain median would say 0
    assert daily_rate(history(0.3, 0.3), NOW) is None  # too little history


def test_forecast_tells_whether_the_limit_lasts():
    ok = forecast(weekly(0.2, 5), history(0.1, 0.1, 0.1), NOW)
    assert ok.status == "ok" and round(ok.projected, 2) == 0.7 and ok.basis_days == 3 and ok.runs_out_at is None
    tight = forecast(weekly(0.5, 5), history(0.1, 0.1, 0.1), NOW)
    assert tight.status == "tight"
    over = forecast(weekly(0.6, 5), history(0.2, 0.2, 0.2), NOW)
    assert over.status == "over" and over.runs_out_at == int(NOW + 2 * DAY) and round(over.budget, 2) == 0.08


def test_forecast_falls_back_to_this_weeks_pace():
    pace = forecast(weekly(0.6, 4), {}, NOW)
    assert pace.basis_days == 0 and round(pace.daily, 2) == 0.2 and pace.status == "over"
    assert forecast(weekly(0.1, 6.5), {}, NOW) is None  # the week has only just started
    assert forecast(LimitWindow(name="seven_day", utilization=0.1), {}, NOW) is None  # no reset known
    full = forecast(weekly(1.0, 2), history(0.2, 0.2, 0.2), NOW)
    assert full.status == "over" and full.runs_out_at is None
