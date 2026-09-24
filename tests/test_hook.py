"""The SessionEnd hook script, run for real with /bin/sh against a stub package: it queues the session Claude Code
names on stdin (atomically, one file per session) and starts one runner that calls the warehouse with the queue."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "warehouse_hook.sh"
STUB = """import os, sys
from pathlib import Path
q = Path(sys.argv[sys.argv.index("--session-queue") + 1])
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(repr((sys.argv[1:], sorted((f.name, f.read_text().strip()) for f in q.glob("*")))) + "\\n")
for f in q.glob("*"):
    f.unlink()
"""


@pytest.mark.skipif(not shutil.which("sh"), reason="needs a POSIX sh")
def test_the_hook_queues_the_ended_session_and_runs_once(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src" / "session_analytics").mkdir(parents=True)
    shutil.copy(HOOK, repo / "scripts" / "warehouse_hook.sh")
    (repo / "src" / "session_analytics" / "__init__.py").write_text("")
    (repo / "src" / "session_analytics" / "__main__.py").write_text(STUB)
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = dict(os.environ, HOME=str(home), TMPDIR=str(tmp), STUB_LOG=str(tmp_path / "stub.log"))
    transcript = "/x/projects/p/11111111-2222-3333-4444-555555555555.jsonl"
    hook_input = json.dumps({"session_id": "s", "transcript_path": transcript, "hook_event_name": "SessionEnd"})
    done = subprocess.run(["sh", str(repo / "scripts" / "warehouse_hook.sh")], input=hook_input, text=True, env=env,
                          timeout=30)
    assert done.returncode == 0  # returns at once: the load runs in the background
    deadline = time.time() + 30
    while (tmp / "convo-analysis-warehouse.lock").exists() and time.time() < deadline:
        time.sleep(0.1)
    (argv, queued), = [eval(line) for line in (tmp_path / "stub.log").read_text().splitlines()]  # noqa: S307
    assert argv[:4] == ["warehouse", "--load=auto", "--clickhouse=auto", "--session-queue"]
    assert queued == [("11111111-2222-3333-4444-555555555555", transcript)]
    assert not list((home / "claude-session-exports" / "_warehouse" / "queue").iterdir())
