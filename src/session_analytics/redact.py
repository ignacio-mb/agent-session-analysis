"""Best-effort secret scrubbing for exported text.

Transcripts hold everything a session saw: tool output can contain a token that
was printed once and forgotten. Every free-text field an export writes (prompts,
commands, tool inputs, error previews, answers) goes through `redact()` unless
the caller passes --no-redact. It is pattern-based, so it is a speed bump for
the common credential shapes, not a guarantee.
"""

from __future__ import annotations

import re

MASK = "‹redacted›"

_PATTERNS = [
    # PEM private keys (multi-line)
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"),
    # Anthropic / OpenAI style keys
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_\-]{20,}"),
    # GitHub tokens
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    # Slack tokens and webhooks
    re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+"),
    # AWS access key ids
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Google API keys
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    # Stripe keys
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[0-9A-Za-z]{16,}"),
    # JWTs
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    # Bearer tokens in headers
    re.compile(r"(?i)(?<=bearer\s)[A-Za-z0-9._~+/\-]{20,}=*"),
]

# key=value / key: value where the key names a secret; keeps the key, masks the value.
_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_\-]*(?:password|passwd|secret|token|api[_\-]?key|access[_\-]?key|private[_\-]?key|"
    r"client[_\-]?secret|auth)[A-Z0-9_\-]*)(\s*[:=]\s*)(['\"]?)([^\s'\"&,;]{6,})(\3)"
)

# credentials embedded in URLs: scheme://user:pass@host
_URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]{3,})(@)")

# secret-looking query parameters
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|apikey|key|secret|signature|sig|password|auth|code)=)([^&#\s]{6,})"
)


def redact(text):
    if not text or not isinstance(text, str):
        return text
    out = text
    for pat in _PATTERNS:
        out = pat.sub(MASK, out)
    out = _URL_CREDS.sub(lambda m: m.group(1) + MASK + m.group(3), out)
    out = _QUERY_SECRET.sub(lambda m: m.group(1) + MASK, out)

    def _assign(m):
        value = m.group(4)
        # Leave obvious non-secrets alone (env var references, placeholders, booleans).
        if value.startswith("$") or value.lower() in {"true", "false", "none", "null", "required", "optional"}:
            return m.group(0)
        return m.group(1) + m.group(2) + m.group(3) + MASK + m.group(5)

    out = _ASSIGNMENT.sub(_assign, out)
    return out


class Redactor:
    """Callable that redacts when enabled and counts what it touched."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.hits = 0

    def __call__(self, text):
        if not self.enabled or not text or not isinstance(text, str):
            return text
        out = redact(text)
        if out != text:
            self.hits += 1
        return out
