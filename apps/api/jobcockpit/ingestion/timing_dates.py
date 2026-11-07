from __future__ import annotations

_COMMON_COOP_TERM_MONTHS = frozenset({8, 12, 16})


def _month_span_duration(
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
    *,
    end_is_boundary: bool = False,
) -> int:
    elapsed_months = (end_year - start_year) * 12 + end_month - start_month
    return elapsed_months if end_is_boundary else elapsed_months + 1


def _end_date_for_month_span(year: int, start_month: int, duration_months: int) -> str:
    end_month_index = start_month + duration_months - 1
    end_year = year + (end_month_index - 1) // 12
    end_month = ((end_month_index - 1) % 12) + 1
    return f"{end_year:04d}-{end_month:02d}-{_last_day_of_month(end_year, end_month):02d}"


def _last_day_of_month(year: int, month: int) -> int:
    if month == 2:
        return 29 if _is_leap_year(year) else 28
    if month in {4, 6, 9, 11}:
        return 30
    return 31


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
