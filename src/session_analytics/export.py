"""Parse -> analyze -> write every requested format for one session."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import render_csv, render_html, render_md
from .analyze import analyze
from .parse import parse_session
from .pricing import Pricing
from .redact import Redactor
from .util import local_str

FORMATS = ("json", "md", "html", "csv")
DEFAULT_ROOT = "~/claude-session-exports"


def default_root():
    return Path(os.path.expanduser(os.environ.get("SESSION_ANALYTICS_OUT") or DEFAULT_ROOT))


def _slug(s, n=40):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")
    return (s[:n] or "session").rstrip("-")


def default_out_dir(a, root=None):
    """<root>/<project>/<start yyyy-mm-dd_HHMM>_<short id>/ — stable, so re-exporting overwrites."""
    s = a["session"]
    start = local_str(s["start_ms"], "%Y-%m-%d_%H%M") if s.get("start_ms") else "undated"
    return Path(root or default_root()) / _slug(s["project_name"]) / f"{start}_{s['id'][:8]}"


def parse_formats(value):
    if not value or value == "all":
        return list(FORMATS)
    out = [f.strip().lower() for f in value.split(",") if f.strip()]
    bad = [f for f in out if f not in FORMATS]
    if bad:
        raise ValueError(f"unknown format(s): {', '.join(bad)} (choose from {', '.join(FORMATS)} or all)")
    return out


def export_session(path, out_dir=None, formats=FORMATS, full=False, redact=True, pricing=None,
                   current_id=None, now_ms=None, root=None, own_only=False, checks=(), skill_sources=()):
    session = parse_session(path, own_only=own_only)
    redactor = Redactor(enabled=redact)
    a = analyze(session, pricing or Pricing(), redactor=redactor, full=full, current_id=current_id,
                now_ms=now_ms if now_ms is not None else time.time() * 1000, checks=checks,
                skill_sources=skill_sources)
    out = Path(out_dir) if out_dir else default_out_dir(a, root)
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    if "html" in formats:
        paths["dashboard"] = render_html.write(a, out / "report.html", title=f"{a['session']['title']} — session report")
    if "md" in formats:
        (out / "report.md").write_text(render_md.report(a), encoding="utf-8")
        paths["report"] = str(out / "report.md")
    if "json" in formats:
        with open(out / "session.json", "w", encoding="utf-8") as fh:
            json.dump(a, fh, ensure_ascii=False, indent=1, default=str)
        paths["json"] = str(out / "session.json")
    if "csv" in formats:
        csv_dir = out / "csv"
        csv_dir.mkdir(exist_ok=True)
        render_csv.write_all(a, str(csv_dir))
        paths["csv"] = str(csv_dir)
    # The summary names every file, so it is written last and always.
    summary = render_md.summary(a, paths)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    paths["summary"] = str(out / "summary.md")
    return {"out_dir": str(out), "paths": paths, "analytics": a, "summary": summary}
