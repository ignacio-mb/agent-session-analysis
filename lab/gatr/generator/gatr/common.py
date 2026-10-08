"""Shared constants and helpers: the seed, the calendar, random streams, formatting.

Every random draw comes from rng(name): a stream of its own per part of the world, derived from the one SEED, so a change
in one part of the generator doesn't reshuffle every other part.
"""

from __future__ import annotations

import zlib
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

SEED = 19980417  # the day GATR was founded
START = date(2022, 1, 1)
END = date(2026, 9, 30)
DAYS = pd.date_range(START, END, freq="D")
N_DAYS = len(DAYS)

# Plant milestones
P2_OPEN = date(2022, 7, 1)
P3_ACQUIRED = date(2024, 3, 1)
P3_LEGACY_HISTORY_FROM = date(2023, 7, 1)  # the legacy export carries history from before the acquisition
P3_GO_LIVE = date(2025, 5, 5)  # P3 lines 1 and 2 move to the ERP; bakery stays on the legacy system
ERP_UPGRADE = date(2024, 7, 1)  # status codes change meaning, credit notes move to `returns`


def rng(name: str) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence([SEED, zlib.crc32(name.encode())]))


def d(s: str) -> date:
    return date.fromisoformat(s)


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def easter(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    dd, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - dd - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = (h + l_ - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def holidays() -> set[date]:
    out = set()
    for y in range(START.year, END.year + 1):
        for md in ("01-01", "03-24", "04-02", "05-01", "05-25", "06-20", "07-09", "08-17", "10-12", "11-20", "12-08",
                   "12-25"):
            out.add(date.fromisoformat(f"{y}-{md}"))
        e = easter(y)
        out |= {e - timedelta(days=48), e - timedelta(days=47), e - timedelta(days=2)}
    return out


HOLIDAYS = holidays()

# Planned shutdowns (a red herring: zero production, energy and labour drop, inventory drawn down)
SHUTDOWNS = {
    "P1": [(d("2023-01-09"), d("2023-01-22")), (d("2025-01-06"), d("2025-01-12"))],
    "P2": [(d("2024-01-15"), d("2024-01-26"))],
    "P3": [(d("2025-01-13"), d("2025-01-19"))],
}


def in_shutdown(plant: str, day: date) -> bool:
    return any(a <= day <= b for a, b in SHUTDOWNS.get(plant, []))


def month_index(day) -> int:
    return (day.year - START.year) * 12 + day.month - 1


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def clip(x, lo, hi):
    return max(lo, min(hi, x))


def choice(r: np.random.Generator, seq, p=None):
    return seq[int(r.choice(len(seq), p=p))]


def weighted(r: np.random.Generator, items: dict):
    keys = list(items)
    w = np.array([items[k] for k in keys], dtype=float)
    return keys[int(r.choice(len(keys), p=w / w.sum()))]


def fake_tax_id(r: np.random.Generator, prefix: int = 30) -> str:
    """A CUIT-shaped number whose check digit is deliberately wrong, so it can never be a real one."""
    body = f"{prefix}{int(r.integers(70_000_000, 79_999_999)):08d}"
    weights = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    s = sum(int(c) * w for c, w in zip(body, weights))
    check = 11 - s % 11
    check = 0 if check == 11 else 9 if check == 10 else check
    wrong = (check + 1 + int(r.integers(0, 8))) % 10
    if wrong == check:
        wrong = (check + 1) % 10
    return f"{body[:2]}-{body[2:]}-{wrong}"


def season(family: str, day) -> float:
    """Demand seasonality multiplier by product family and day of year."""
    m = day.month
    doy = day.timetuple().tm_yday
    if family == "CHIPS":
        base = 1 + 0.12 * np.cos(2 * np.pi * (doy - 15) / 365)
        if m == 12:
            base *= 1.25
    elif family == "CRACKERS":
        base = 1 + 0.06 * np.cos(2 * np.pi * (doy - 200) / 365)
    elif family in ("SAUCE_JAR", "SAUCE_SACHET"):
        base = 1 + 0.15 * np.cos(2 * np.pi * (doy - 20) / 365)
        if m == 12:
            base *= 1.18
    elif family == "FROZEN":
        base = 1 + 0.22 * np.cos(2 * np.pi * (doy - 196) / 365)  # winter peak, summer dip
    elif family == "BAKERY":
        base = 1 + 0.05 * np.cos(2 * np.pi * (doy - 180) / 365)
    else:
        base = 1.0
    return float(base)


def fmt_ts(x) -> str | None:
    if x is None or (isinstance(x, float) and np.isnan(x)) or x is pd.NaT:
        return None
    return pd.Timestamp(x).strftime("%Y-%m-%d %H:%M:%S")


def fmt_date(x) -> str | None:
    if x is None or x is pd.NaT or (isinstance(x, float) and np.isnan(x)):
        return None
    return pd.Timestamp(x).strftime("%Y-%m-%d")
