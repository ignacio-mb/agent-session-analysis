"""How source systems write values, and the registry of every corruption rule applied (answer_key/corruptions.md is
written from it, with the counts each rule actually touched)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

EXCEL_EPOCH = date(1899, 12, 30)


@dataclass
class Rule:
    id: str
    tables: str
    columns: str
    source: str
    description: str
    affected: int = 0
    total: int = 0
    notes: list = field(default_factory=list)


class Registry:
    def __init__(self):
        self.rules: dict[str, Rule] = {}

    def rule(self, id, tables, columns, source, description):
        if id not in self.rules:
            self.rules[id] = Rule(id, tables, columns, source, description)
        return self.rules[id]

    def count(self, id, affected, total):
        r = self.rules[id]
        r.affected += int(affected)
        r.total += int(total)


# ---- value formatters --------------------------------------------------------------------------------------------------

def iso_date(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) or x is pd.NaT else pd.Timestamp(x).strftime(
        "%Y-%m-%d")


def iso_ts(x, sep=" "):
    if x is None or x is pd.NaT or (isinstance(x, float) and np.isnan(x)):
        return None
    return pd.Timestamp(x).strftime(f"%Y-%m-%d{sep}%H:%M:%S")


def dmy(x):
    return None if x is None or x is pd.NaT else pd.Timestamp(x).strftime("%d/%m/%Y")


def dmy_hm(x):
    return None if x is None or x is pd.NaT else pd.Timestamp(x).strftime("%d/%m/%Y %H:%M")


def mdy(x):
    return None if x is None or x is pd.NaT else pd.Timestamp(x).strftime("%m/%d/%y")


def excel_serial(x):
    if x is None or x is pd.NaT:
        return None
    return str((pd.Timestamp(x).date() - EXCEL_EPOCH).days)


ART = timezone(timedelta(hours=-3))  # Argentina, no DST since 2009


def local_to_utc(x: datetime) -> datetime:
    return x + timedelta(hours=3)


def epoch_ms_from_local(x) -> str | None:
    if x is None or x is pd.NaT:
        return None
    t = pd.Timestamp(x).to_pydatetime().replace(tzinfo=ART)
    return str(int(t.timestamp() * 1000))


def num(x, decimals=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    s = f"{x:.{decimals}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def comma(x, decimals=2, thousands=False):
    """Spanish number format: decimal comma, optional dot thousands separator."""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    s = f"{x:,.{decimals}f}" if thousands else f"{x:.{decimals}f}"
    if thousands:
        s = s.replace(",", "§").replace(".", ",").replace("§", ".")
    else:
        s = s.replace(".", ",")
    if "," in s:
        s = s.rstrip("0").rstrip(",")
    return s


def mojibake(s):
    if s is None:
        return None
    try:
        return s.encode("utf-8").decode("latin-1")
    except UnicodeError:
        return s


def strip_accents(s):
    if s is None:
        return None
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def lstrip_zeros(s):
    if s is None:
        return None
    t = str(s).lstrip("0")
    return t if t else "0"


# ---- CSV ---------------------------------------------------------------------------------------------------------------

def _field(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if np.isnan(v):
            return ""
        v = repr(v)
    elif not isinstance(v, str):
        v = str(v)
    if v == "":
        return '""'
    if any(c in v for c in ',"\n\r') or v[0] == " " or v[-1] == " ":
        return '"' + v.replace('"', '""') + '"'
    return v


def write_csv(path, columns, rows):
    """Writes a CSV where None is an unquoted empty field (NULL to Postgres) and '' is "" (an empty string)."""
    import gzip
    import io
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            buf = io.TextIOWrapper(gz, encoding="utf-8", newline="")
            buf.write(",".join(columns) + "\n")
            out = []
            for row in rows:
                out.append(",".join(_field(v) for v in row))
                if len(out) >= 50_000:
                    buf.write("\n".join(out) + "\n")
                    out = []
            if out:
                buf.write("\n".join(out) + "\n")
            buf.flush()
            buf.detach()
