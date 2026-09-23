"""Small pure functions: timestamps, stats, shell parsing, redaction, pricing."""

import json

import pytest

from session_analytics import util
from session_analytics.analyze import categorize_error, primary_program, programs_of
from session_analytics.pricing import Pricing, normalize_model
from session_analytics.redact import redact


def test_parse_ts():
    assert util.parse_ts("2026-09-01T10:00:00.123Z") == pytest.approx(1788256800123.0)
    assert util.parse_ts("2026-09-01T10:00:00.1234567Z") == pytest.approx(1788256800123.4567, abs=1)
    assert util.parse_ts("nope") is None and util.parse_ts(None) is None


def test_stats_helpers():
    assert util.percentile([5, 1, 3, 2, 4], 50) == 3
    assert util.describe([])["count"] == 0
    assert util.merged_span_ms([(0, 10), (5, 15), (20, 25)]) == 20
    assert util.patch_counts([{"lines": ["-a", "+b", "+c", " d"]}]) == (2, 1)
    assert util.count_lines("a\nb\n") == 2 and util.count_lines("a\nb") == 2 and util.count_lines("") == 0
    assert util.fmt_duration(61_000) == "1m 01s" and util.fmt_tokens(1_234_567) == "1.23M"


@pytest.mark.parametrize("cmd,progs,primary", [
    ("cd ~/x && python3 - <<'EOF'\nimport os\nprint(1)\nEOF\necho done", ["cd", "python3", "echo"], "python3"),
    ("uv run pytest -q tests/ | tail -5", ["uv", "tail"], "uv"),
    ("FOO=1 git -C repo status --short; gh pr view 12", ["git", "gh"], "git"),
    ('python3 -c "import a; print(1)" && make test', ["python3", "make"], "python3"),
    ("for f in *.py; do wc -l $f; done", ["wc"], "wc"),
    ("if [ -f x ]; then cat x; fi", ["test", "cat"], "test"),
])
def test_programs_of(cmd, progs, primary):
    assert [p for p, _ in programs_of(cmd)] == progs
    assert primary_program(cmd)[0] == primary


def test_subcommands():
    assert programs_of("git -C repo status")[0] == ("git", "status")
    assert programs_of("uv run pytest")[0] == ("uv", "run pytest")
    assert programs_of("python3 -m http.server 8000")[0] == ("python3", "-m http.server")


@pytest.mark.parametrize("text,category", [
    ("Exit code 2\nboom", "exit_code"),
    ("<tool_use_error>String to replace not found in file.</tool_use_error>", "edit_no_match"),
    ("<tool_use_error>File has not been read yet. Read it first before writing to it.</tool_use_error>",
     "file_not_read_first"),
    ("Output does not match required schema: root", "schema_mismatch"),
    ("This session is isolated in the worktree /x", "worktree_isolation"),
    ("<tool_use_error>InputValidationError: bad</tool_use_error>", "input_validation"),
    ("Something odd", "other"),
])
def test_categorize_error(text, category):
    assert categorize_error(text) == category


def test_redact():
    assert "sk-ant-" not in redact("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    assert redact("AWS AKIAABCDEFGHIJKLMNOP") == "AWS ‹redacted›"
    assert redact("password=hunter2hunter2") == "password=‹redacted›"
    assert redact("export API_KEY=$FROM_ENV") == "export API_KEY=$FROM_ENV"
    assert redact("https://user:s3cretpass@host/x") == "https://user:‹redacted›@host/x"
    assert "abcdef1234" not in redact("GET /x?access_token=abcdef1234&page=2")
    assert redact("plain words stay") == "plain words stay"
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----"
    assert redact(pem) == "‹redacted›"


def test_pricing_lookup_and_cost(tmp_path):
    p = Pricing()
    assert normalize_model("claude-opus-5[1m]") == "claude-opus-5"
    assert p.lookup("claude-opus-5-5")["input"] == 4.0  # longest prefix wins over claude-opus-5
    assert p.lookup("claude-haiku-4-5-20251001")["prefix"] == "claude-haiku-4-5"
    assert p.cost("mystery-model", 1, 1) is None
    assert p.cost("<synthetic>", 100, 100)["total"] == 0
    c = p.cost("claude-opus-5", input_tokens=1000, output_tokens=50, cache_read=10000, cache_write_1h=2000)
    assert c["total"] == pytest.approx(0.005 + 0.00125 + 0.005 + 0.02)
    assert p.cost("claude-fable-5-1", cache_read=1_000_000)["cache_read"] == pytest.approx(0.25)
    f = tmp_path / "prices.json"
    f.write_text(json.dumps({"claude-opus-5": {"input": 1, "output": 2}}))
    q = Pricing(str(f))
    assert q.lookup("claude-opus-5")["input"] == 1 and q.overridden == ["claude-opus-5"]
