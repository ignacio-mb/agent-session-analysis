"""Self-contained HTML dashboard: one file, inline CSS/JS/data, works offline from file://."""

from __future__ import annotations

import html
import json
from pathlib import Path

from . import __version__

TEMPLATES = Path(__file__).parent / "templates"


def embed_json(data):
    """JSON safe inside <script type="application/json">: no `</script>`, no HTML comment openers."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return (text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render(data, app="session.js", title=None):
    tpl = (TEMPLATES / "report.html").read_text(encoding="utf-8")
    parts = {
        "{{CSS}}": (TEMPLATES / "base.css").read_text(encoding="utf-8"),
        "{{LIB}}": (TEMPLATES / "lib.js").read_text(encoding="utf-8"),
        "{{APP}}": (TEMPLATES / app).read_text(encoding="utf-8"),
        "{{TITLE}}": html.escape(title or "Claude session report"),
        "{{VERSION}}": __version__,
    }
    for k, v in parts.items():
        tpl = tpl.replace(k, v)
    # Data last, so nothing inside it is ever mistaken for a placeholder.
    return tpl.replace("{{DATA}}", embed_json(data))


def write(data, path, app="session.js", title=None):
    Path(path).write_text(render(data, app=app, title=title), encoding="utf-8")
    return str(path)
