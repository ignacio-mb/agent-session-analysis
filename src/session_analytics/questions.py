"""The user interview: every question Claude put to the user, how it was asked, and what came back.

Questions arrive through two channels. AskUserQuestion asks with options, the recommended one first by
convention, and is answered by picking an option, by typing something else, or not at all. Prose asks in the
reply that ends a turn and is answered by the next prompt. A `[CHECKPOINT]` block printed with no
AskUserQuestion behind it is recorded as well: skills like rde define that as a stop that did not happen.

Each question gets:
  topic    from the skill's interview taxonomy (checks/<skill>.json, "interview") or the generic one below
  form     confirm | choice | multi | prose | checkpoint
  outcome  recommended | other option | picked (no recommendation offered) | typed | typed + picked |
           no preference | declined | unanswered | interrupted | error; replied | unanswered for prose
  wait     how long the answer took
  flags    against the rules skills set for their questions: a recommendation, offered first; two or more
           options; measured numbers behind a decision; plain language; asked once; not after an error

Built for tuning a skill's interview: which topics get asked, how often and when; whether people pick the
recommended option (always: the question could be a decision shown instead; rarely: the default is wrong);
what they type when no option fits; what they skip, decline or get asked twice.
"""

from __future__ import annotations

import re
from collections import Counter

from . import semantics, util

RECOMMENDED_RE = re.compile(r"\(\s*recommended\s*\)", re.I)
NO_PREFERENCE = {"", "[no preference]", "no preference", "(no preference)"}
AFFIRM_RE = re.compile(r"^\W*(yes|yep|ok\b|okay|sure|go\b|go ahead|proceed|publish|looks (right|good)|approve|accept|"
                       r"include|keep|do it|ship|build|continue|confirm)", re.I)
NEGATE_RE = re.compile(r"^\W*(no\b|not\b|skip|stop|needs|cancel|exclude|leave|don'?t|wait|review|hold|later)", re.I)
OFFER_RE = re.compile(r"^\W*(want me to|shall i|should i|do you want( me)? to|would you like( me)? to|ready for me to|"
                      r"can i|may i|ok(ay)? to)\b", re.I)
DECLINED_RE = re.compile(r"the user said:?\s*(.*)$", re.S | re.I)
CODE_RE = re.compile(r"(?i)\bselect\b[^?]{0,200}\bfrom\b|```|\{\s*\"[\w-]+\"\s*:")
NUMBER_RE = re.compile(r"\d")
WORD_RE = re.compile(r"[a-z0-9]+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9*_\"'(\[`])")
ABBREVIATION = re.compile(r"\b(e\.g|i\.e|etc|vs|approx|incl|excl|no)\.", re.I)
CHECKPOINT_BLOCK = re.compile(r"\[CHECKPOINT\][^\n]*\n(?P<body>(?:.*\n?){0,14})")
STOP_WORDS = {"the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "is", "it", "be", "should", "i", "you",
              "your", "this", "that", "what", "which", "how", "do", "does", "with", "as", "me", "we", "are", "at", "by"}
FAILED = ("declined", "unanswered", "interrupted", "error")

# Topics for a skill that declares none: what kind of thing the question asks the user to supply.
DEFAULT_TOPICS = [
    {"id": "permission", "label": "Permission to go ahead",
     "match": r"\b(proceed|go ahead|ok(ay)? to|can i|may i|should i (go|proceed|continue|start|create|publish|push|merge|"
              r"commit|deploy|delete|run|apply))\b|approve|confirm|ready to"},
    {"id": "scope", "label": "What to do first or next",
     "match": r"\b(first|next|focus|priorit\w*|which (part|area|one|approach|option|of these))\b|what should i"},
    {"id": "definition", "label": "A rule or definition",
     "match": r"\bcount(s|ed)?\b|what counts|defin\w+|\brules?\b|\binclude\b|\bexclude\b|threshold"},
    {"id": "setup", "label": "Where and how things run",
     "match": r"environment|production|staging|profile|instance|database|schema|folder|\bpath\b|where should|"
              r"which (repo|branch|account|project|directory)"},
    {"id": "data-handling", "label": "Sensitive data", "match": r"personal|\bpii\b|e-?mails?|secret|credential|mask"},
    {"id": "preference", "label": "A preference",
     "match": r"prefer|style|format|\bname\b|how (do|would) you (like|want)"},
]


class Taxonomy:
    """How one skill names the topics of its questions (checks/<skill>.json "interview")."""

    def __init__(self, spec=None, skill=None):
        spec = spec or {}
        self.skill = skill
        self.source = "skill" if spec.get("topics") else "default"
        self.topics = []
        for t in spec.get("topics") or DEFAULT_TOPICS:
            self.topics.append({"id": t["id"], "label": t.get("label") or t["id"], "rx": re.compile(t["match"], re.I),
                                "once": bool(t.get("once")), "evidence": bool(t.get("evidence")),
                                "must_ask": bool(t.get("must_ask"))})
        self.jargon = [re.compile(r"\b" + j + r"\b", re.I) for j in spec.get("jargon") or ()]
        self.jargon_words = list(spec.get("jargon") or ())
        self.prose_is_a_miss = spec.get("prose") == "avoid"

    def classify(self, header, question):
        text = f"{header or ''} {question or ''}"
        for t in self.topics:
            if t["rx"].search(text):
                return t
        return None

    def label(self, topic_id):
        return next((t["label"] for t in self.topics if t["id"] == topic_id), "Other" if topic_id == "other" else topic_id)

    def order(self):
        return [t["id"] for t in self.topics] + ["other", "offer"]


def _answer_for(answers, question, n_questions):
    """The answer recorded for one question: answers are keyed by question text."""
    if not answers:
        return None
    if question in answers:
        return answers[question]
    norm = " ".join((question or "").split())
    for k, v in answers.items():
        if " ".join(str(k).split()) == norm:
            return v
    if n_questions == 1 and len(answers) == 1:
        return next(iter(answers.values()))
    return None


def _form(options, multi):
    if multi:
        return "multi"
    labels = [o.get("label") or "" for o in options]
    if len(labels) == 2 and any(AFFIRM_RE.search(x) for x in labels) and any(NEGATE_RE.search(x) for x in labels):
        return "confirm"
    return "choice"


def _outcome(status, value, options, multi):
    """(outcome, picked labels, typed text)."""
    if status == "denied":
        return "declined", [], None
    if status == "interrupted":
        return "interrupted", [], None
    if status == "error":
        return "error", [], None
    if status != "ok" or value is None:
        return "unanswered", [], None
    vals = value if isinstance(value, list) else [value]
    vals = [str(v).strip() for v in vals if v is not None and str(v).strip()]
    if not vals or all(v.lower() in NO_PREFERENCE for v in vals):
        return "no preference", [], None
    labels = [o.get("label") for o in options]
    picked = [v for v in vals if v in labels]
    typed = [v for v in vals if v not in labels]
    rec = [o.get("label") for o in options if o.get("recommended")]
    if typed and picked:
        return "typed + picked", picked, "; ".join(typed)
    if typed:
        return "typed", [], "; ".join(typed)
    if rec and sorted(picked) == sorted(rec):
        return "recommended", picked, None
    return ("other option" if rec else "picked"), picked, None


def prose_questions(text):
    """Questions a reply asks in prose: sentences ending in '?', outside code blocks, tables and quotes."""
    text = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    out = []
    for line in text.split("\n"):
        ln = line.strip()
        if not ln or ln.startswith(("|", ">", "#")) or line.startswith("    "):
            continue
        ln = re.sub(r"^([-*+]|\d+[.)])\s+", "", ln).replace("**", "").replace("__", "")
        protected = ABBREVIATION.sub(lambda m: m.group(0).replace(".", "․"), ln)
        for sent in SENTENCE_SPLIT.split(protected):
            sent = sent.strip().replace("․", ".")
            if sent.endswith("?") and len(sent) >= 12 and re.search(r"[A-Za-z]{3}", sent):
                out.append(sent)
    return out


def _checkpoint_blocks(text):
    """`[CHECKPOINT]` blocks in a reply: {decision, options, recommendation}, per the block's own lines."""
    out = []
    for m in CHECKPOINT_BLOCK.finditer(text or ""):
        body = m.group("body")
        dec = re.search(r"^\s*Decision:\s*(.+)$", body, re.M)
        if not dec:
            continue
        opts = re.findall(r"^\s*([A-H])[.)]\s+(.+)$", body, re.M)
        rec = re.search(r"^\s*Recommendation:\s*(.+)$", body, re.M)
        out.append({"decision": dec.group(1).strip(), "options": opts, "recommendation": rec.group(1).strip() if rec else None})
    return out


def _content_words(text):
    return {w for w in WORD_RE.findall((text or "").lower()) if w not in STOP_WORDS and len(w) > 2}


def _similar(a, b):
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def raw_questions(s, reqs, calls, text):
    """Every question put to the user, in order, not yet classified (see classify()).

    `text(value, limit)` is the exporter's redacting clipper.
    """
    clip = text

    def text(v, limit):  # noqa: F811 - missing stays missing rather than becoming ""
        return clip(v, limit) if v not in (None, "") else None

    out = []
    by_id = {c.id: c for c in calls}
    req_by_key = {r.key: r for r in reqs}
    turns = [t for t in s.turns if not t.inherited]
    asks_by_turn = {}
    for c in sorted((c for c in calls if c.name == "AskUserQuestion"), key=lambda c: c.ts_call or 0):
        asks_by_turn.setdefault((c.turn, c.scope, c.agent_id), []).append(c)
        qs = c.facts.get("questions") or [
            {"header": q.get("header"), "question": q.get("question"), "multi": bool(q.get("multiSelect")),
             "options": [{"label": o.get("label"), "description": o.get("description"), "preview": bool(o.get("preview"))}
                         for o in q.get("options") or () if isinstance(o, dict)]}
            for q in c.input.get("questions") or () if isinstance(q, dict)]
        answers = c.facts.get("answers") or {}
        notes = c.facts.get("annotations") or {}
        feedback = None
        if c.status == "denied":
            m = DECLINED_RE.search(c.result_preview or "")
            feedback = text(m.group(1).strip(), 400) if m and m.group(1).strip() else None
        context = req_by_key.get(c.request_key)
        context_text = context.text_full if context is not None else ""
        for i, q in enumerate(qs):
            options = [dict(o, recommended=bool(RECOMMENDED_RE.search(o.get("label") or "")))
                       for o in q.get("options") or ()]
            rec = next((j for j, o in enumerate(options) if o["recommended"]), None)
            value = _answer_for(answers, q.get("question"), len(qs))
            outcome, picked, typed = _outcome(c.status, value, options, q.get("multi"))
            note = notes.get(q.get("question")) if isinstance(notes, dict) else None
            out.append({
                "qid": f"{c.id}#{i}", "kind": "ask", "t": c.ts_call, "turn": c.turn, "scope": c.scope,
                "agent": c.agent_id, "call": c.id, "batch_size": len(qs), "batch_index": i,
                "header": text(q.get("header"), 60), "question": text(q.get("question"), 700),
                "_question": q.get("question") or "", "_context": context_text,
                "options": [{"label": text(o.get("label"), 160), "description": text(o.get("description"), 300),
                             "preview": o.get("preview"), "recommended": o["recommended"],
                             "chosen": o.get("label") in picked} for o in options],
                "multi": bool(q.get("multi")), "form": _form(options, q.get("multi")),
                "recommended_index": rec, "recommended_label": text(options[rec]["label"], 160) if rec is not None else None,
                "status": c.status, "outcome": outcome,
                "answer": text("; ".join(value) if isinstance(value, list) else value, 500) if value is not None else None,
                "typed": text(typed, 500), "notes": text(note.get("notes"), 400) if isinstance(note, dict) else None,
                "preview_seen": bool(isinstance(note, dict) and note.get("preview")),
                "feedback": feedback,
                "wait_ms": (c.ts_result - c.ts_call) if c.ts_result is not None and c.ts_call is not None else None,
                "reply": None,
            })
    # Prose: questions in the reply that ends a turn, answered by the next prompt.
    next_prompt = {}
    for i, t in enumerate(turns):
        nxt = next((u for u in turns[i + 1:] if u.trigger in ("prompt", "command")), None)
        next_prompt[t.index] = nxt
    for r in sorted((r for r in reqs if r.scope == "main" and not r.inherited and r.stop_reason == "end_turn"
                     and r.text_full), key=lambda r: r.ts_first or 0):
        blocks = _checkpoint_blocks(r.text_full)
        sentences = prose_questions(r.text_full)
        if not blocks and not sentences:
            continue
        nxt = next_prompt.get(r.turn)
        reply = None
        if nxt is not None:
            reply = nxt.text or (f"/{nxt.command} {nxt.command_args or ''}".strip() if nxt.command else "")
        t_end = r.ts_last or r.ts_first
        wait = (nxt.ts_start - t_end) if nxt is not None and nxt.ts_start is not None and t_end is not None else None
        asked_after = [c for c in asks_by_turn.get((r.turn, "main", None), ()) if (c.ts_call or 0) >= (r.ts_first or 0)]
        for j, b in enumerate(blocks):
            if asked_after:
                continue  # the block is the context of the AskUserQuestion that follows it
            letters = {k: v for k, v in b["options"]}
            rec_letter = re.match(r"\s*([A-H])\b", b["recommendation"] or "")
            options = [{"label": text(f"{k}. {v}", 200), "description": None, "preview": False,
                        "recommended": bool(rec_letter and rec_letter.group(1) == k), "chosen": False}
                       for k, v in letters.items()]
            out.append(_prose_row(r, f"{r.key}#cp{j}", "checkpoint", b["decision"], options, reply, wait, text,
                                  recommended=next((o["label"] for o in options if o["recommended"]), None)))
        for j, sent in enumerate(sentences):
            out.append(_prose_row(r, f"{r.key}#q{j}", "prose", sent, [], reply, wait, text))
    # A checkpoint block right before an AskUserQuestion is that question's context.
    for r in reqs:
        if r.scope == "main" and r.text_full and _checkpoint_blocks(r.text_full):
            for q in out:
                if q["kind"] == "ask" and q["call"] in r.tool_use_ids:
                    q["checkpoint_block"] = True
    out.sort(key=lambda q: (q["t"] or 0, q["qid"]))
    for q in out:
        q.setdefault("checkpoint_block", False)
        c = by_id.get(q["call"]) if q["call"] else None
        q["after_error"] = _after_error(c, calls) if c is not None else False
    return out


def _prose_row(r, qid, kind, question, options, reply, wait, text, recommended=None):
    offer = bool(OFFER_RE.search(question or ""))
    if reply is None:
        outcome = "unanswered"
    elif kind == "prose" and offer and AFFIRM_RE.search(reply or ""):
        outcome = "accepted"
    elif kind == "prose" and offer and NEGATE_RE.search(reply or ""):
        outcome = "turned down"
    else:
        outcome = "replied"
    return {"qid": qid, "kind": kind, "t": r.ts_last or r.ts_first, "turn": r.turn, "scope": r.scope, "agent": None,
            "call": None, "batch_size": None, "batch_index": None, "header": None, "question": text(question, 700),
            "_question": question or "", "_context": r.text_full,
            "options": options, "multi": False, "form": "offer" if offer else kind,
            "recommended_index": next((i for i, o in enumerate(options) if o["recommended"]), None),
            "recommended_label": recommended, "status": "ok" if reply is not None else "pending",
            "outcome": outcome, "answer": None, "typed": None, "notes": None, "preview_seen": False, "feedback": None,
            "wait_ms": wait, "reply": text(reply, 240) if reply is not None else None}


def _after_error(c, calls):
    """The question came right after a failed tool call in the same thread: a clarification, not a plan."""
    before = [x for x in calls if x.scope == c.scope and x.agent_id == c.agent_id and x.turn == c.turn
              and x.ts_call is not None and c.ts_call is not None and x.ts_call < c.ts_call and x.name != "AskUserQuestion"]
    before.sort(key=lambda x: x.ts_call)
    return any(x.status == "error" for x in before[-3:])


def classify(qs, taxonomy):
    """Topic, flags and re-asks for questions of one run (or of a session with no skill), in order."""
    earlier = []
    for q in qs:
        if q["form"] == "offer":
            # "Want me to …?" at the end of a reply offers a next step; it is not one of the skill's questions.
            t = None
            q["topic"], q["topic_label"] = "offer", "Offers a next step (in prose)"
        else:
            t = taxonomy.classify(q.get("header"), q.get("_question"))
            q["topic"] = t["id"] if t else "other"
            q["topic_label"] = t["label"] if t else "Other"
        q["taxonomy"] = taxonomy.source
        flags = []
        body = q.get("_question") or ""
        if q["kind"] in ("ask", "checkpoint"):
            opts = q["options"]
            if len(opts) < 2 and not q["multi"] and q["kind"] == "ask":
                flags.append("fewer than two options")
            if q["recommended_index"] is None and not q["multi"] and len(opts) >= 2:
                flags.append("no recommendation")
            elif q["recommended_index"] not in (None, 0):
                flags.append("recommendation not first")
        if q["kind"] == "checkpoint":
            flags.append("checkpoint with no AskUserQuestion")
        if q["kind"] == "prose" and taxonomy.prose_is_a_miss:
            flags.append("asked in prose")
        if t and t["evidence"] and not NUMBER_RE.search(body) and not NUMBER_RE.search(q.get("_context") or ""):
            flags.append("no measured numbers")
        words = " ".join([body] + [o.get("label") or "" for o in q["options"]])
        hits = [w for w, rx in zip(taxonomy.jargon_words, taxonomy.jargon) if rx.search(words)]
        if hits:
            flags.append("jargon: " + ", ".join(hits))
        if CODE_RE.search(body):
            flags.append("code in the question")
        if q.get("after_error"):
            flags.append("after an error")
        q["reask_of"] = None
        for e in earlier:
            once_again = t is not None and t["once"] and e["topic"] == q["topic"]
            sim = _similar(e.get("_question"), body)
            if once_again or sim >= 0.6:
                q["reask_of"] = e["qid"]
                flags.append("asked again" if once_again else "like an earlier question")
                break
        q["flags"] = flags
        # What the question is about in data-engineering terms: a topic and a layer (semantics/questions.json).
        q.update(semantics.load().classify(q.get("header"), body, q["topic"]))
        q["words"] = len(body.split())
        q["has_numbers"] = bool(NUMBER_RE.search(body))
        earlier.append(q)
    return qs


def public(q, **extra):
    """A question as exported: private working fields dropped."""
    return dict({k: v for k, v in q.items() if not k.startswith("_")}, **extra)


def summarize(qs):
    """Counts and rates over a list of classified questions."""
    asks = [q for q in qs if q["kind"] == "ask"]
    answered = [q for q in asks if q["outcome"] not in FAILED]
    offered = [q for q in answered if q["recommended_label"] and not q["multi"]]
    offered_ids = {q["qid"] for q in offered}
    picked_rec = [q for q in offered if q["outcome"] == "recommended"]
    # One wait per AskUserQuestion call (its questions are answered together); prose waits run to the next
    # prompt, which includes time away, so they are kept apart.
    waits = [q["wait_ms"] for q in asks if q["wait_ms"] is not None and q["batch_index"] == 0
             and q["outcome"] not in FAILED]
    prose_waits = [q["wait_ms"] for q in qs if q["kind"] != "ask" and q["wait_ms"] is not None]
    by_topic = {}
    for q in qs:
        b = by_topic.setdefault(q["topic"], {"topic": q["topic"], "label": q["topic_label"], "asked": 0, "prose": 0,
                                             "outcomes": Counter(), "offered": 0, "recommended": 0, "waits": [],
                                             "typed": [], "answers": Counter(), "flags": Counter()})
        b["asked" if q["kind"] == "ask" else "prose"] += 1
        b["outcomes"][q["outcome"]] += 1
        if q["qid"] in offered_ids:
            b["offered"] += 1
            b["recommended"] += q["outcome"] == "recommended"
        if q["kind"] == "ask" and q["wait_ms"] is not None and q["outcome"] not in FAILED:
            b["waits"].append(q["wait_ms"])
        if q["typed"]:
            b["typed"].append(q["typed"])
        if q["kind"] == "ask" and q["answer"] and q["outcome"] not in FAILED:
            b["answers"][RECOMMENDED_RE.sub("", q["answer"]).strip()] += 1
        for f in q["flags"]:
            b["flags"][f.split(":")[0]] += 1
    topics = []
    for b in by_topic.values():
        waits_b = b.pop("waits")
        topics.append(dict(b, outcomes=dict(b["outcomes"].most_common()), answers=dict(b["answers"].most_common(6)),
                           flags=dict(b["flags"].most_common()), typed=b["typed"][:8],
                           recommended_rate=util.ratio(b["recommended"], b["offered"]),
                           wait_p50_ms=util.percentile(waits_b, 50)))
    topics.sort(key=lambda b: -(b["asked"] + b["prose"]))
    outcomes = Counter(q["outcome"] for q in qs)
    flags = Counter(f.split(":")[0] for q in qs for f in q["flags"])
    return {
        "total": len(qs), "asked": len(asks), "calls": len({q["call"] for q in asks}),
        "prose": sum(1 for q in qs if q["kind"] == "prose"),
        "checkpoints_unasked": sum(1 for q in qs if q["kind"] == "checkpoint"),
        "answered": len(answered), "recommended_offered": len(offered), "recommended_picked": len(picked_rec),
        "recommended_rate": util.ratio(len(picked_rec), len(offered)),
        "typed": sum(1 for q in asks if q["outcome"] in ("typed", "typed + picked")),
        "no_preference": outcomes["no preference"], "declined": outcomes["declined"],
        "unanswered": sum(1 for q in asks if q["outcome"] == "unanswered"),
        "prose_unanswered": sum(1 for q in qs if q["kind"] != "ask" and q["outcome"] == "unanswered"),
        "flagged": sum(1 for q in qs if q["flags"]), "reasked": sum(1 for q in qs if q["reask_of"]),
        "wait_p50_ms": util.percentile(waits, 50), "wait_max_ms": max(waits) if waits else None,
        "wait_total_ms": sum(waits) if waits else 0, "prose_wait_p50_ms": util.percentile(prose_waits, 50),
        "outcomes": dict(outcomes.most_common()), "flags": dict(flags.most_common()), "topics": topics,
        "de_topics": dict(Counter(q.get("de_topic_label") for q in qs if q.get("de_topic_label")).most_common()),
        "layers": dict(Counter(q.get("layer_label") for q in qs if q.get("layer_label")).most_common()),
    }
