"""The Metabase build script's semantic layer, offline: its MBQL names only the model's columns and metrics, and
the Question topics tab fits the grid."""

import importlib.util
import re
from pathlib import Path

from session_analytics import semantics, warehouse

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "metabase_dashboard.py"


def load():
    spec = importlib.util.spec_from_file_location("metabase_dashboard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fields(node):
    if isinstance(node, (list, tuple)):
        if len(node) == 3 and node[0] == "field" and isinstance(node[2], str):
            yield node[2]
        for x in node:
            yield from fields(x)
    elif isinstance(node, dict):
        for v in node.values():
            yield from fields(v)


def test_the_model_columns_are_the_views():
    md = load()
    from session_analytics import clickhouse
    select = warehouse.VIEWS.split("CREATE VIEW v_interview_questions AS")[1].split("\nFROM questions q")[0]
    missing = [c for c in md.MODEL_COLUMNS if not re.search(rf"(\.|AS ){c}\b", select)]
    # the run's prompt comes from skill_runs, joined by the model; person is the shared ClickHouse's: whose sessions
    assert missing == ["prompt_key", "person"]
    assert "AS person" in clickhouse.VIEWS["v_interview_questions"]
    assert "prompt_key" in warehouse.TABLES["skill_runs"][1][-2]
    assert md.MODEL_SQL.startswith("SELECT i.*, r.prompt_key FROM v_interview_questions i LEFT JOIN skill_runs r")
    assert "r.session_id = i.session_id" in md.model_sql_clickhouse("sessions")


def test_topic_cards_use_only_model_columns_and_metrics():
    md = load()
    metric_ids = {k: n for n, (k, *_) in enumerate(md.METRICS, 100)}
    types = dict.fromkeys(md.MODEL_COLUMNS, "type/Text")
    for _key, _name, _description, agg in md.METRICS:
        assert set(fields(agg)) <= set(md.MODEL_COLUMNS)
    for _key, _name, _display, (kind, q), _vis, filters, _pos in md.topic_cards():
        assert set(filters) <= set(md.FILTER_NAMES)
        if kind == "sql":
            assert md.SKILL in q and all(f"{{{{{f}}}}}" in q for f in filters)
            continue
        assert {md.MODEL_FILTER_COLUMNS[f] for f in filters} <= set(md.MODEL_COLUMNS)
        stage = md.topic_stage(q, "rde", types, metric_ids)
        assert stage["filters"] == [["=", {}, ["field", {"base-type": "type/Text"}, "skill"], "rde"]]
        assert set(fields(stage)) <= set(md.MODEL_COLUMNS)
        for agg in stage.get("aggregation", ()):
            assert agg[0] == "metric" and agg[2] in metric_ids.values() and len(agg[1]["lib/uuid"]) == 36
        for order in stage.get("order-by", ()):
            if order[2][0] == "aggregation":  # points at one of this card's aggregations
                assert order[2][2] in {a[1]["lib/uuid"] for a in stage["aggregation"]}


def test_the_matrix_has_a_column_per_layer_and_the_tab_fits_the_grid():
    md = load()
    _, layers = semantics.load().dimensions()
    sql = md.crosstab_sql()
    assert all(f"AS {md._ident(lay['id'])}" for lay in layers) and sql.count("FILTER") == len(layers)
    cells = set()
    boxes = [pos for *_, pos in md.topic_cards()]
    for x, y, w, h in boxes:
        assert x + w <= 24
        box = {(c, r) for c in range(x, x + w) for r in range(y, y + h)}
        assert not cells & box, (x, y, w, h)
        cells |= box
    assert any(x + w == 24 for x, _, w, _ in boxes)


def test_every_native_card_has_clickhouse_sql():
    md = load()
    ch = md.clickhouse_sql("sessions")
    keys = [key for key, *_ in md.CARDS] + [key for key, _n, _d, q, *_ in md.topic_cards() if q[0] == "sql"]
    assert sorted(keys) == sorted(ch)
    for key, sql in ch.items():
        # nothing Postgres-only: ::numeric would be Decimal(10, 0) in ClickHouse
        assert not re.search(r"::|percentile_cont|string_agg|interval '|FILTER \(WHERE|NOT EXISTS|LATERAL", sql), key
        assert re.search(r"\bFROM sessions\.", sql), key  # tables are database.table in ClickHouse
    for key, _name, _display, (kind, q), *_ in md.topic_cards("clickhouse", "sessions"):
        if kind == "sql":
            assert q == ch[key]


def native_cards(md, dialect="postgres", db=None):
    ch = md.clickhouse_sql(db) if dialect == "clickhouse" else {}
    grid = md.check_grid(dialect, db)
    return [(key, tab, ch.get(key, sql) if key != "checkgrid" else sql, filters, pos)
            for key, tab, _name, _display, sql, _vis, filters, pos in [*md.CARDS, grid]]


def test_every_card_is_the_skills_and_maps_exactly_the_filters_it_names():
    md = load()
    for dialect, db in (("postgres", None), ("clickhouse", "sessions")):
        for key, _tab, sql, filters, _pos in native_cards(md, dialect, db):
            named = {f for f in md.FILTER_NAMES if f"{{{{{f}}}}}" in sql}
            # Person is the shared ClickHouse warehouse's column: every card of the skill's runs takes it there
            person = {"person"} if dialect == "clickhouse" and key != "loaded" else set()
            assert named == set(filters) | person, (dialect, key)
            assert (md.SKILL in sql) == (key != "loaded"), (dialect, key)
            query = md.native_query(sql, "rde")
            assert md.SKILL not in query["stages"][0]["native"] and set(query["stages"][0]["template-tags"]) == named


def test_every_tab_fits_the_grid():
    md = load()
    boxes = {}
    for _key, tab, _sql, _filters, pos in native_cards(md):
        boxes.setdefault(tab, []).append(pos)
    boxes[md.TOPIC_TAB] = [pos for *_, pos in md.topic_cards()]
    assert set(boxes) == set(md.ALL_TABS)
    for tab, positions in boxes.items():
        cells = set()
        for x, y, w, h in positions:
            box = {(c, r) for c in range(x, x + w) for r in range(y, y + h)}
            assert x + w <= 24 and not cells & box, (tab, (x, y, w, h))
            cells |= box


def test_the_check_grid_has_a_column_per_check():
    md = load()
    from session_analytics import checks
    ids = [c["id"] for c in checks.load(skill="rde")]
    _key, _tab, _name, _display, sql, vis, _filters, _pos = md.check_grid()
    assert "tests-before-first-run" in ids
    assert all(f"AS {md._ident(i)}" in sql for i in ids)
    assert [vis["column_settings"][f'["name","{md._ident(i)}"]']["column_title"] for i in ids] == ids


def test_hand_added_text_cards_stay_and_push_the_tab_down():
    md = load()
    note = {"id": 7, "card_id": None, "dashboard_tab_id": 1, "row": 0, "col": 0, "size_x": 24, "size_y": 2}
    lower = {"id": 8, "card_id": None, "dashboard_tab_id": 1, "row": 30, "col": 0, "size_x": 24, "size_y": 2}
    card = {"id": 9, "card_id": 5, "dashboard_tab_id": 1, "row": 2, "col": 0, "size_x": 24, "size_y": 6}
    kept, top = md._kept_text_cards({"dashcards": [card, lower, note]})
    assert [dc["id"] for dc in kept] == [8, 7] and top == {1: 2}


def test_filters_take_their_values_from_the_cards_and_person_only_on_clickhouse():
    md = load()
    ids = {"everyrun": 1, "sessions": 2}
    by_card = {f: ids[k] for f, k in md.VALUES_CARDS.items()}
    ch = {p["id"]: p for p in md._parameters(by_card, person=True)}
    assert list(ch) == ["person", "version", "prompt", "session", "de_topic", "layer"]
    assert [p["id"] for p in md._parameters(by_card, person=False)] == ["version", "prompt", "session", "de_topic", "layer"]
    assert ch["session"]["values_source_config"] == {"card_id": 2, "value_field": ["field", "session", {"base-type": "type/Text"}]}
    assert ch["person"]["values_source_config"]["card_id"] == 1
    assert ch["layer"]["values_source_config"]["values"][2] == "Tests"
    everyrun = md.clickhouse_sql("sessions")["everyrun"]
    assert all(f" AS {c}" in everyrun for c in ("person", "version", "prompt", "session"))


def test_the_session_label_is_the_same_in_the_list_and_the_filter():
    md = load()
    for dialect, sql in (("postgres", next(c[4] for c in md.CARDS if c[0] == "sessions")),
                         ("clickhouse", md.clickhouse_sql("sessions")["sessions"])):
        assert f"{md.session_label('s', dialect)} AS session" in sql
        assert f"{md.session_label('ss', dialect)} = {{{{session}}}}" in sql
