"""The shop's calendar. Every "today" in Arth is the date in India, whatever timezone the machine
or the database server runs in; db/schema.sql's shop_today() is the same rule on the Postgres side.

India has no daylight saving, so a fixed +05:30 offset is exact and needs no tz database on Windows.
"""

from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def today() -> date:
    return datetime.now(IST).date()


PERIOD_LABEL = {"today": "Aaj", "yesterday": "Kal", "this_week": "Is hafte", "last_week": "Pichhle hafte",
                "this_month": "Is mahine", "last_month": "Pichhle mahine", "this_year": "Is saal",
                "last_year": "Pichhle saal"}
PERIOD_LABEL_EN = {"today": "Today", "yesterday": "Yesterday", "this_week": "This week", "last_week": "Last week",
                   "this_month": "This month", "last_month": "Last month", "this_year": "This financial year",
                   "last_year": "Last financial year"}


def fy_start(d: date) -> date:
    """1 April of the financial year d falls in: "is saal" for an Indian shop."""
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def period_range(period: str | None, default: str = "this_month", lang: str = "hi") -> tuple[date, date, str]:
    """"this_month", "last_week", "2026-02", "2026-10-05", ... -> (from, to inclusive, label in lang)."""
    labels = PERIOD_LABEL_EN if lang == "en" else PERIOD_LABEL
    today_ = today()
    month_start = today_.replace(day=1)
    period = period or default
    if period == "today":
        return today_, today_, labels["today"]
    if period == "yesterday":
        d = today_ - timedelta(days=1)
        return d, d, labels["yesterday"]
    if period == "this_week":
        return today_ - timedelta(days=today_.weekday()), today_, labels["this_week"]
    if period == "last_week":
        end = today_ - timedelta(days=today_.weekday() + 1)
        return end - timedelta(days=6), end, labels["last_week"]
    if period == "last_month":
        end = month_start - timedelta(days=1)
        return end.replace(day=1), end, labels["last_month"]
    if period == "this_year":
        return fy_start(today_), today_, labels["this_year"]
    if period == "last_year":
        start = fy_start(today_)
        return start.replace(year=start.year - 1), start - timedelta(days=1), labels["last_year"]
    if len(period) == 7:
        try:
            start = date.fromisoformat(period + "-01")
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            return start, end, f"{start:%b %Y}"
        except ValueError:
            pass
    if len(period) == 10:
        try:
            d = date.fromisoformat(period)
            return d, d, f"{d:%d %b}"
        except ValueError:
            pass
    return month_start, today_, labels["this_month"]


def _month_back(d: date) -> date:
    last = d.replace(day=1) - timedelta(days=1)                   # last day of the month before
    return last.replace(day=min(d.day, last.day))


def previous_range(start: date, end: date) -> tuple[date, date]:
    """The span to compare with: a whole month against the whole month before, a month so far against
    the same days of the month before, a financial year so far against the same dates a year earlier,
    and any other span against the same number of days just before it."""
    if start.month == 4 and start.day == 1 and (end - start).days > 31:
        return start.replace(year=start.year - 1), end.replace(year=end.year - 1)
    if start.day == 1 and (end.year, end.month) == (start.year, start.month) and end.day > 7:
        whole = (end + timedelta(days=1)).day == 1
        return _month_back(start), start - timedelta(days=1) if whole else _month_back(end)
    days = (end - start).days + 1
    return start - timedelta(days=days), start - timedelta(days=1)
