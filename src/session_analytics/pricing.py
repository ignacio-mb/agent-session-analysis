"""List-price cost estimation for the API calls recorded in a transcript.

Rates are USD per million tokens. Cache writes are priced from the input rate
(1.25x for the 5-minute TTL, 2x for the 1-hour TTL); cache reads carry their own
rate because two models break the usual 0.1x rule (Fable 5.1 is 0.025x, Opus 5.5
is 0.05x). Thinking is billed as output and is already inside `output_tokens`.

This table reproduces the `costUSD` Claude Code itself writes into a transcript's
`cost-state` event to the cent (checked against every session that has one), so
the estimate and the reported figure should only diverge by calls Claude Code
makes without writing them to the transcript (titles, compaction summaries...).

Override or extend it with a JSON file, `--pricing prices.json` or the
SESSION_ANALYTICS_PRICING environment variable:

    {"claude-opus-5": {"input": 5, "output": 25, "cache_read": 0.5}}
"""

from __future__ import annotations

import json
import os

CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0
WEB_SEARCH_USD_PER_REQUEST = 0.01  # $10 per 1,000 searches
FAST_MODE_MULT = 2.0  # documented for Opus 5 / Opus 5.5 fast mode

# prefix -> (input, output, cache_read, source)
DEFAULT_PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, "claude-api reference 2026-06-24"),
    "claude-mythos-5-1": (10.0, 50.0, 0.25, "claude-api reference 2026-06-24 (cache-read rate open at launch)"),
    "claude-fable-5": (10.0, 50.0, 1.00, "claude-api reference 2026-06-24"),
    "claude-mythos-5": (10.0, 50.0, 1.00, "claude-api reference 2026-06-24"),
    "claude-opus-5-5": (4.0, 20.0, 0.20, "claude-api reference 2026-06-24"),
    "claude-opus-5": (5.0, 25.0, 0.50, "claude-api reference 2026-06-24"),
    "claude-opus-4-8": (5.0, 25.0, 0.50, "claude-api reference 2026-06-24"),
    "claude-opus-4-7": (5.0, 25.0, 0.50, "claude-api reference 2026-06-24"),
    "claude-opus-4-6": (5.0, 25.0, 0.50, "claude-api reference 2026-06-24"),
    "claude-opus-4-5": (5.0, 25.0, 0.50, "legacy list price (unverified)"),
    "claude-opus-4-1": (15.0, 75.0, 1.50, "legacy list price (unverified)"),
    "claude-opus-4": (15.0, 75.0, 1.50, "legacy list price (unverified)"),
    "claude-sonnet-5": (2.0, 10.0, 0.20, "claude-api reference 2026-06-24"),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, "claude-api reference 2026-06-24"),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30, "legacy list price (unverified)"),
    "claude-sonnet-4": (3.0, 15.0, 0.30, "legacy list price (unverified)"),
    "claude-3-7-sonnet": (3.0, 15.0, 0.30, "legacy list price (unverified)"),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, "claude-api reference 2026-06-24"),
    "claude-3-5-haiku": (0.80, 4.0, 0.08, "legacy list price (unverified)"),
    "claude-3-haiku": (0.25, 1.25, 0.03, "legacy list price (unverified)"),
}

COMPONENTS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h", "web_search")


def normalize_model(model):
    """`claude-opus-5[1m]` -> `claude-opus-5`; long-context variants carry no premium."""
    if not model:
        return ""
    return str(model).split("[", 1)[0].strip().lower()


class Pricing:
    def __init__(self, overrides=None):
        self.table = {k: {"input": v[0], "output": v[1], "cache_read": v[2], "source": v[3]}
                      for k, v in DEFAULT_PRICES.items()}
        self.overridden = []
        path = overrides or os.environ.get("SESSION_ANALYTICS_PRICING")
        if path:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                data = json.load(fh)
            for prefix, rates in data.items():
                entry = dict(self.table.get(prefix, {}))
                entry.update(rates)
                entry.setdefault("cache_read", entry["input"] * 0.1)
                entry["source"] = f"override ({path})"
                self.table[prefix.lower()] = entry
                self.overridden.append(prefix)
        self._prefixes = sorted(self.table, key=len, reverse=True)
        self._cache = {}

    def lookup(self, model):
        m = normalize_model(model)
        if m in self._cache:
            return self._cache[m]
        hit = None
        for prefix in self._prefixes:
            if m.startswith(prefix):
                hit = dict(self.table[prefix], prefix=prefix)
                break
        self._cache[m] = hit
        return hit

    def cost(self, model, input_tokens=0, output_tokens=0, cache_read=0, cache_write_5m=0,
             cache_write_1h=0, cache_write_unsplit=0, web_search=0, speed=None):
        """Cost components in USD, or None when the model has no known price."""
        if model == "<synthetic>":
            # Client-side placeholder for an API error; nothing was billed.
            return dict({c: 0.0 for c in COMPONENTS}, total=0.0)
        p = self.lookup(model)
        if p is None:
            return None
        mult = FAST_MODE_MULT if speed == "fast" else 1.0
        per = 1e6
        out = {
            "input": input_tokens * p["input"] / per * mult,
            "output": output_tokens * p["output"] / per * mult,
            "cache_read": cache_read * p["cache_read"] / per * mult,
            # An unsplit cache write (older transcripts) is priced at the API's default 5m TTL.
            "cache_write_5m": (cache_write_5m + cache_write_unsplit) * p["input"] * CACHE_WRITE_5M_MULT / per * mult,
            "cache_write_1h": cache_write_1h * p["input"] * CACHE_WRITE_1H_MULT / per * mult,
            "web_search": web_search * WEB_SEARCH_USD_PER_REQUEST,
        }
        out["total"] = sum(out[c] for c in COMPONENTS)
        return out

    def describe(self):
        rows = []
        for prefix in sorted(self.table):
            p = self.table[prefix]
            rows.append({
                "model_prefix": prefix,
                "input": p["input"],
                "output": p["output"],
                "cache_read": p["cache_read"],
                "cache_write_5m": round(p["input"] * CACHE_WRITE_5M_MULT, 4),
                "cache_write_1h": round(p["input"] * CACHE_WRITE_1H_MULT, 4),
                "source": p.get("source"),
            })
        return {
            "unit": "USD per million tokens",
            "cache_write_5m_multiplier": CACHE_WRITE_5M_MULT,
            "cache_write_1h_multiplier": CACHE_WRITE_1H_MULT,
            "web_search_usd_per_request": WEB_SEARCH_USD_PER_REQUEST,
            "fast_mode_multiplier": FAST_MODE_MULT,
            "overridden": self.overridden,
            "rates": rows,
        }
