"""What a question is about, in data-engineering terms: a topic (the kind of concern — sources, modeling,
business logic, quality, delivery, platform…) and a layer (where in the stack — source, staging, modeling,
semantic, presentation, platform, or cross-cutting).

The rules live in semantics/questions.json: regular expressions tried in order, first on the question's header
(the agent's own short label for it, usually the most precise signal), then on its text. The first topic and the
first layer that match win; a question matching none falls back to its interview topic. `by` records which of
header / question / fallback decided, so the mapping can be audited question by question.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT = Path(__file__).resolve().parents[2] / "semantics" / "questions.json"
_CACHE = {}


class Semantics:
    def __init__(self, path=None):
        spec = json.loads(Path(path or DEFAULT).read_text(encoding="utf-8"))
        self.topics = [dict(t, rx=re.compile(t["match"], re.I)) for t in spec["topics"]]
        self.layers = [dict(t, rx=re.compile(t["match"], re.I)) for t in spec["layers"]]
        self.fallback = spec.get("fallback") or {}
        self.topic_label = {t["id"]: t["label"] for t in self.topics} | {"other": "Other"}
        self.layer_label = {t["id"]: t["label"] for t in self.layers}

    @staticmethod
    def _first(rules, text):
        if not text:
            return None
        return next((r["id"] for r in rules if r["rx"].search(text)), None)

    def classify(self, header, question, interview_topic=None):
        """{de_topic, de_topic_label, layer, layer_label, by}"""
        fb = self.fallback.get(interview_topic or "other") or self.fallback.get("other") or ["other", "cross-cutting"]
        topic, by_t = self._first(self.topics, header), "header"
        if topic is None:
            topic, by_t = self._first(self.topics, question), "question"
        if topic is None:
            topic, by_t = fb[0], "fallback"
        layer, by_l = self._first(self.layers, header), "header"
        if layer is None:
            layer, by_l = self._first(self.layers, question), "question"
        if layer is None:
            layer, by_l = fb[1], "fallback"
        return {"de_topic": topic, "de_topic_label": self.topic_label.get(topic, topic), "layer": layer,
                "layer_label": self.layer_label.get(layer, layer), "semantics_by": f"{by_t}/{by_l}"}

    def dimensions(self):
        """The topics and layers as rows, in order, for a warehouse dimension table."""
        return ([{"id": t["id"], "label": t["label"], "description": t.get("description"), "sort_order": i}
                 for i, t in enumerate(self.topics)] + [{"id": "other", "label": "Other", "description": None,
                                                         "sort_order": len(self.topics)}],
                [{"id": t["id"], "label": t["label"], "description": t.get("description"), "sort_order": i}
                 for i, t in enumerate(self.layers)])


def load(path=None):
    key = str(path or DEFAULT)
    if key not in _CACHE:
        _CACHE[key] = Semantics(path)
    return _CACHE[key]
