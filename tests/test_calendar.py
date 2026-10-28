import pytest
from app.calendar import canonical_date, persian_date, build_calendar_context, render_persian_answer, persian_time


def test_simulated_day_relative_dates_and_jalali_conversion_are_deterministic():
    context = build_calendar_context("2026-11-16", "برای فردا ۱۴۰۵/۸/۲۶ یا 2026-11-17")
    assert context["reference_day_persian"] == "۲۵ آبان ۱۴۰۵"
    assert context["relative_dates"]["فردا"] == {"gregorian": "2026-11-17", "persian": "۲۶ آبان ۱۴۰۵"}
    assert context["relative_dates"]["پس‌فردا"]["gregorian"] == "2026-11-18"
    assert canonical_date("۱۴۰۵/۸/۲۶") == "2026-11-17"
    assert persian_date("2026-03-21") == "۱ فروردین ۱۴۰۵"


def test_invalid_dates_are_rejected_and_not_invented():
    with pytest.raises(ValueError): canonical_date("1405-13-02")
    with pytest.raises(ValueError): canonical_date("فردا")
    assert build_calendar_context("2026-11-16", "2026-99-99")["recognized_dates"] == {}


def test_final_presentation_uses_jalali_dates_persian_digits_and_no_inline_codes():
    result = render_persian_answer("ورود 2026-11-17؛ مبلغ 50.1 IRT برای per_room_per_night. [S2]")
    assert result == "ورود ۲۶ آبان ۱۴۰۵؛ مبلغ ۵۰.۱ تومان برای هر اتاق در هر شب."


@pytest.mark.parametrize("clock,expected", [("16:30", "۴:۳۰ عصر"), ("15:00", "۳ بعدازظهر"), ("00:00", "۱۲ نیمه‌شب"), ("07:05", "۷:۰۵ صبح")])
def test_natural_clock_display(clock, expected):
    assert persian_time(clock) == expected


def test_clock_presenter_does_not_reinterpret_iso_timestamps_or_timezone_offsets():
    with pytest.raises(ValueError): persian_time("2026-11-17T16:30:00+03:30")
    with pytest.raises(ValueError): persian_time("+03:30")
    assert "+۰۳:۳۰" in render_persian_answer("زمان فنی +03:30")
    assert render_persian_answer("حرکت ساعت 16:30 و ورود 15:00") == "حرکت ساعت ۴:۳۰ عصر و ورود ۳ بعدازظهر"


def test_explicit_timestamp_is_rendered_as_one_date_time_without_offset_confusion():
    assert render_persian_answer("حرکت 2026-11-17T16:30:00+03:30") == "حرکت ۲۶ آبان ۱۴۰۵، ساعت ۴:۳۰ عصر"


@pytest.mark.parametrize("raw,expected", [
    ("07:30 صبح", "۷:۳۰ صبح"), ("08:10 شب", "۸:۱۰ شب"),
    ("16:30 عصر", "۴:۳۰ عصر"), ("15:00 صبح", "۳ بعدازظهر"),
    ("07:30 صبح صبح", "۷:۳۰ صبح"), ("08:10 شب صبح", "۸:۱۰ شب"),
])
def test_existing_twelve_hour_period_is_preserved_once_and_twenty_four_hour_is_canonical(raw, expected):
    assert render_persian_answer(raw) == expected
