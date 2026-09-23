"""Small helpers shared by the parser, the analytics and the renderers.

Everything here is stdlib-only and Python 3.9 compatible: the skill runs the
package with whatever `python3` the machine has, and macOS still ships 3.9.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

_FRACTION_OVERFLOW = re.compile(r"^(.*\.\d{6})\d+(.*)$")


def parse_ts(value):
    """ISO-8601 timestamp (Claude Code writes `...123Z`) -> epoch milliseconds, or None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # 3.9's fromisoformat rejects more than six fractional digits.
    m = _FRACTION_OVERFLOW.match(s)
    if m:
        s = m.group(1) + m.group(2)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000.0


def iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def local_str(ms, fmt="%Y-%m-%d %H:%M:%S"):
    if ms is None:
        return "—"
    return datetime.fromtimestamp(ms / 1000.0).astimezone().strftime(fmt)


def percentile(values, p):
    """Nearest-rank percentile of an unsorted list; None when empty."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if p <= 0:
        return vals[0]
    k = max(0, min(len(vals) - 1, int(math.ceil(p / 100.0 * len(vals))) - 1))
    return vals[k]


def describe(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"count": 0, "sum": 0, "min": None, "p50": None, "p90": None, "p99": None, "max": None, "mean": None}
    total = sum(vals)
    return {
        "count": len(vals),
        "sum": total,
        "min": min(vals),
        "p50": percentile(vals, 50),
        "p90": percentile(vals, 90),
        "p99": percentile(vals, 99),
        "max": max(vals),
        "mean": total / len(vals),
    }


def fmt_duration(ms):
    if ms is None:
        return "—"
    ms = float(ms)
    if ms < 1000:
        return f"{ms:.0f} ms"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f} s"
    m, s = divmod(int(round(s)), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h:02d}h"


def fmt_tokens(n):
    if n is None:
        return "—"
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.2f}{unit}" if abs(n) < 10 * div else f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def fmt_usd(x):
    if x is None:
        return "—"
    if abs(x) < 0.01 and x != 0:
        return f"${x:.4f}"
    return f"${x:,.2f}"


def fmt_pct(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def ratio(num, den):
    return (num / den) if den else None


_SYS_BLOCK = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>\s*", re.I)


def clean_prompt(text):
    """What the user typed: without the <system-reminder> blocks the harness wraps around a prompt."""
    return _SYS_BLOCK.sub("", text or "").strip()


def text_of(content):
    """Plain text of a message `content`: a string, or the `text` blocks of a block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
        return "\n".join(parts)
    return ""


def result_text(content):
    """Text of a tool_result `content` (string, or text blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            (b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def result_stats(content):
    """(characters, image blocks, tool_reference names) of a tool_result `content`."""
    if isinstance(content, str):
        return len(content), 0, []
    chars, images, refs = 0, 0, []
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                chars += len(b.get("text") or "")
            elif t == "image":
                images += 1
            elif t == "tool_reference":
                if b.get("tool_name"):
                    refs.append(b["tool_name"])
    return chars, images, refs


def count_lines(s):
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def patch_counts(hunks):
    """(+lines, -lines) of a structuredPatch-style hunk list."""
    added = removed = 0
    for h in hunks or ():
        if not isinstance(h, dict):
            continue
        for line in h.get("lines") or ():
            if not isinstance(line, str):
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
    return added, removed


def one_line(s, limit):
    """Collapse whitespace and truncate for previews."""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        return s[: max(0, limit - 1)] + "…"
    return s


def clip(s, limit):
    """Truncate keeping newlines (for multi-line previews)."""
    if s is None:
        return ""
    if not isinstance(s, str):
        s = str(s)
    if limit and len(s) > limit:
        return s[: max(0, limit - 1)] + "…"
    return s


def top(counter, n=None):
    items = sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return items[:n] if n else items


def merged_span_ms(intervals):
    """Total length of the union of [start, end] intervals (parallel tool calls overlap)."""
    spans = sorted((a, b) for a, b in intervals if a is not None and b is not None and b >= a)
    total, cur_a, cur_b = 0.0, None, None
    for a, b in spans:
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total
