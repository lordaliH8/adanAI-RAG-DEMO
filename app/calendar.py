"""Deterministic Persian/Gregorian calendar context and presentation."""
from __future__ import annotations

import re
from datetime import date, timedelta

import jdatetime

ASCII_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
MONTHS = ("فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند")


def canonical_date(value: str) -> str:
    """Accept explicit numeric Gregorian/Jalali dates; return Gregorian ISO."""
    normalized = str(value).translate(ASCII_DIGITS).strip()
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", normalized)
    if not match:
        raise ValueError("تاریخ باید سال، ماه و روز مشخص داشته باشد.")
    year, month, day = map(int, match.groups())
    if 1300 <= year <= 1500:
        result = jdatetime.date(year, month, day).togregorian()
    elif 1900 <= year <= 2200:
        result = date(year, month, day)
    else:
        raise ValueError("تقویم یا سال تاریخ روشن نیست.")
    return result.isoformat()


def persian_date(value: str | date) -> str:
    gregorian = value if isinstance(value, date) else date.fromisoformat(canonical_date(value))
    jalali = jdatetime.date.fromgregorian(date=gregorian)
    return f"{jalali.day} {MONTHS[jalali.month-1]} {jalali.year}".translate(PERSIAN_DIGITS)


def persian_time(value: str, period: str | None = None) -> str:
    normalized = str(value).translate(ASCII_DIGITS).strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?", normalized)
    if not match:
        raise ValueError("ساعت باید ساعت و دقیقه معتبر باشد.")
    hour, minute = int(match[1]), int(match[2])
    canonical_period = "نیمه‌شب" if hour == 0 else "بامداد" if hour < 5 else "صبح" if hour < 12 else "ظهر" if hour == 12 else "بعدازظهر" if hour < 16 else "عصر" if hour < 19 else "شب"
    if period and 1 <= hour <= 12:
        period = {"بعد از ظهر": "بعدازظهر", "نیمه شب": "نیمه‌شب"}.get(period, period)
    else:
        period = canonical_period
    clock = str(hour % 12 or 12) + (f":{minute:02d}" if minute else "")
    return f"{clock} {period}".translate(PERSIAN_DIGITS)


_CLOCK = r"(?<![\dT:+-])(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?![\d:+-])"


def build_calendar_context(as_of: str, text: str = "", dates=(), times=()) -> dict:
    today = date.fromisoformat(as_of)
    relative = {}
    for label, offset in (("دیروز", -1), ("امروز", 0), ("فردا", 1), ("پس‌فردا", 2), ("یک هفته دیگر", 7)):
        current = today + timedelta(days=offset)
        relative[label] = {"gregorian": current.isoformat(), "persian": persian_date(current)}
    mappings = {}
    normalized = text.translate(ASCII_DIGITS)
    candidates = list(re.findall(r"(?<!\d)\d{4}[-/]\d{1,2}[-/]\d{1,2}(?!\d)", normalized)) + list(dates)
    for candidate in candidates[:150]:
        try:
            gregorian = canonical_date(candidate)
            mappings[str(candidate)] = {"gregorian": gregorian, "persian": persian_date(gregorian)}
        except (ValueError, TypeError, OverflowError):
            continue
    months = "|".join(MONTHS)
    for match in re.finditer(rf"(?<!\d)(\d{{1,2}})\s*({months})\s*(1[34]\d{{2}})(?!\d)", normalized):
        day, month, year = match.groups()
        try:
            gregorian = canonical_date(f"{year}-{MONTHS.index(month)+1:02d}-{int(day):02d}")
            mappings[match.group()] = {"gregorian": gregorian, "persian": persian_date(gregorian)}
        except ValueError:
            continue
    clocks = {}
    for candidate in [*re.findall(_CLOCK, normalized), *times][:100]:
        try:
            clocks[candidate] = persian_time(candidate)
        except (ValueError, TypeError):
            continue
    return {"description": "روز مرجع، تاریخ‌های نسبی، تبدیل‌های قطعی تقویم و نمایش طبیعی ساعت؛ تاریخ و زمان دیگری حدس زده نشود.",
            "reference_day": today.isoformat(), "reference_day_persian": persian_date(today),
            "relative_dates": relative, "recognized_dates": mappings, "clock_times": clocks}


def render_persian_answer(text: str) -> str:
    """Format actual explicit dates/digits; never infer facts or new prices."""
    normalized = str(text).translate(ASCII_DIGITS)
    normalized = re.sub(r"\[S\d+\]", "", normalized)
    def replace_timestamp(match):
        try:
            return persian_date(match[1]) + "، ساعت " + persian_time(match[2])
        except ValueError:
            return match.group()
    normalized = re.sub(r"(?<!\d)((?:19|20|21)\d{2}-\d{2}-\d{2})T([0-2]\d:[0-5]\d)(?::[0-5]\d(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?", replace_timestamp, normalized)
    def replace_date(match):
        try:
            return persian_date(match.group())
        except ValueError:
            return match.group()
    normalized = re.sub(r"(?<!\d)(?:19|20|21)\d{2}-\d{2}-\d{2}(?!\d)", replace_date, normalized)
    periods = r"نیمه‌شب|نیمه شب|بعدازظهر|بعد از ظهر|بامداد|صبح|ظهر|عصر|شب"
    clock_with_periods = rf"(?P<clock>{_CLOCK})(?P<periods>(?:[ \t]*(?:{periods})(?![\w\u200c]))*)"
    def replace_clock(match):
        existing = re.findall(periods, match["periods"])
        return persian_time(match["clock"], existing[0] if existing else None)
    normalized = re.sub(clock_with_periods, replace_clock, normalized)
    for machine, natural in {"per_room_per_night": "هر اتاق در هر شب", "per_adult_one_way_from": "کف قیمت یک بزرگسال برای یک مسیر",
                              "per_adult_one_way": "هر بزرگسال برای یک مسیر", "IRR": "ریال", "IRT": "تومان",
                              "snapshot": "گردآوری ثبت‌شده", "SQL": "داده‌های ثبت‌شده"}.items():
        normalized = normalized.replace(machine, natural)
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    return normalized.strip().translate(PERSIAN_DIGITS)
