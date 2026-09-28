# Development

```bash
make test          # pytest on the system Python (3.9) and on the uv default
make smoke         # parse and analyze every session on this machine; reports failures
make render-check  # render the newest session's dashboard in Node and fail on any JS error
make lint
make clickhouse-dev-test  # the whole ClickHouse path on a throwaway local ClickHouse (make clickhouse-dev-down removes it)
```

The code is small and flat: `parse.py` turns transcripts into records, `analyze.py` turns records into the
analytics document, and `render_*.py` and `templates/` are views of that document. The warehouse loaders are
`warehouse.py` (Postgres) and `clickhouse.py`; [warehouse.md](warehouse.md) describes what they write.
