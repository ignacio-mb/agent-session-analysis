"""Inventory what is actually in the transcripts, and flag what this version does not recognise.

Claude Code's transcript format is internal and changes between releases. Run
`session-analytics schema` after upgrading Claude Code: anything listed under
"not recognised" is new data the analytics are not reading yet.
"""

from __future__ import annotations

import json
from collections import Counter

from .locate import side_dir
from .parse import KNOWN_ATTACHMENT_TYPES, KNOWN_EVENT_TYPES, KNOWN_SYSTEM_SUBTYPES


def scan(main_files):
    events, subtypes, attachments, tools, results = Counter(), Counter(), Counter(), Counter(), Counter()
    files = lines = bad = 0
    for main in main_files:
        paths = [main]
        sd = side_dir(main)
        if sd.is_dir():
            paths += [p for p in sorted(sd.rglob("*.jsonl")) if p.name != "journal.jsonl"]
        for p in paths:
            files += 1
            with open(p, encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    if not raw.strip():
                        continue
                    lines += 1
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        bad += 1
                        continue
                    if not isinstance(ev, dict):
                        bad += 1
                        continue
                    t = ev.get("type") or "<none>"
                    events[t] += 1
                    if t == "system":
                        subtypes[ev.get("subtype") or "<none>"] += 1
                    elif t == "attachment":
                        attachments[(ev.get("attachment") or {}).get("type") or "<none>"] += 1
                    elif t == "assistant":
                        for b in (ev.get("message") or {}).get("content") or ():
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                tools[b.get("name") or "?"] += 1
                    elif t == "user" and isinstance(ev.get("toolUseResult"), dict):
                        results.update(ev["toolUseResult"].keys())
    return {
        "sessions": len(main_files), "files": files, "lines": lines, "bad_lines": bad,
        "event_types": dict(events.most_common()),
        "system_subtypes": dict(subtypes.most_common()),
        "attachment_types": dict(attachments.most_common()),
        "tool_names": dict(tools.most_common()),
        "tool_result_keys": dict(results.most_common()),
        "unknown": {
            "event_types": {k: v for k, v in events.items() if k not in KNOWN_EVENT_TYPES},
            "system_subtypes": {k: v for k, v in subtypes.items() if k not in KNOWN_SYSTEM_SUBTYPES},
            "attachment_types": {k: v for k, v in attachments.items() if k not in KNOWN_ATTACHMENT_TYPES},
        },
    }
