#!/usr/bin/env python3
"""Launcher for the session-export skill.

The skill directory is symlinked into ~/.claude/skills/, so this file resolves its
own real path to find the session_analytics package in the repository it came
from. SESSION_ANALYTICS_HOME overrides that when the skill was copied rather than
linked.
"""

import os
import sys
from pathlib import Path

if sys.version_info < (3, 9):
    sys.exit("session-export needs Python 3.9 or newer (found %d.%d)." % sys.version_info[:2])


def _package_root():
    candidates = []
    env = os.environ.get("SESSION_ANALYTICS_HOME")
    if env:
        candidates.append(Path(env).expanduser() / "src")
    # <repo>/skills/session-export/scripts/session_export.py -> <repo>/src
    candidates.append(Path(__file__).resolve().parents[3] / "src")
    for c in candidates:
        if (c / "session_analytics" / "__init__.py").is_file():
            return c
    return None


root = _package_root()
if root is None:
    sys.exit("session-export: cannot find the session_analytics package. Point SESSION_ANALYTICS_HOME at the "
             "convo-analysis checkout, or reinstall the skill with its install.sh (which symlinks it).")
sys.path.insert(0, str(root))

from session_analytics.cli import main  # noqa: E402

sys.exit(main())
