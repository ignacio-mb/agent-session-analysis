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
    select = warehouse.VIEWS.split("CREATE VIEW v_interview_questions AS")[1].split("\nFROM questions q")[0]
    assert [c for c in md.MODEL_COLUMNS if not re.search(rf"(\.|AS ){c}\b", select)] == []
    assert md.MODEL_SQL == "SELECT * FROM v_interview_questions"


def test_topic_cards_use_only_model_columns_and_metrics():
    md = load()
    metric_ids = {k: n for n, (k, *_) in enumerate(md.METRICS, 100)}
    types = dict.fromkeys(md.MODEL_COLUMNS, "type/Text")
    for _key, _name, _description, agg in md.METRICS:
        assert set(fields(agg)) <= set(md.MODEL_COLUMNS)
    for _key, _name, _display, (kind, q), _vis, filters, _pos in md.topic_cards():
        assert set(filters) <= {"skill", "de_topic", "layer"}
        if kind == "sql":
            assert "{{skill}}" in q and all(f"{{{{{f}}}}}" in q for f in filters)
            continue
        assert set(filters) <= set(md.MODEL_COLUMNS)
        stage = md._resolve(q, types, metric_ids, {})
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
