"""Reporting period keys: "2026" (year), "2026-Q4" (quarter), "2026-11" (month)."""

import calendar
import re
from datetime import date
from typing import Tuple

from .errors import ValidationError

_YEAR = re.compile(r"^(\d{4})$")
_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$", re.IGNORECASE)
_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def parse_period(key: str) -> Tuple[str, str, date, date]:
    """key -> (normalised key, period type, first day, last day)."""
    key = (key or "").strip()
    if m := _YEAR.match(key):
        y = int(m.group(1))
        return key, "year", date(y, 1, 1), date(y, 12, 31)
    if m := _QUARTER.match(key):
        y, q = int(m.group(1)), int(m.group(2))
        last_month = q * 3
        return f"{y}-Q{q}", "quarter", date(y, last_month - 2, 1), \
            date(y, last_month, calendar.monthrange(y, last_month)[1])
    if m := _MONTH.match(key):
        y, mo = int(m.group(1)), int(m.group(2))
        return key, "month", date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])
    raise ValidationError(f"Unknown period '{key}'. Use 2026 (year), 2026-Q4 (quarter) or 2026-11 (month).")
