PY39 := /usr/bin/python3

.PHONY: test test39 lint smoke render-check install uninstall warehouse warehouse-check warehouse-up warehouse-down warehouse-psql

test: test39
	uv run --no-project --with pytest python -m pytest -q

# The skill runs with the system python3; keep the package working there.
test39:
	uv run --no-project --with pytest --python $(PY39) python -m pytest -q

lint:
	uv run --no-project --with ruff ruff check .

smoke:
	PYTHONPATH=src python3 scripts/smoke.py

# Build the newest session's dashboard and render every tab, chart and table in Node (needs node).
render-check:
	PYTHONPATH=src python3 -m session_analytics export latest --format html --out .preview --quiet
	node scripts/render_check.js .preview/report.html

install:
	./install.sh

uninstall:
	./install.sh --uninstall

# Every session into the local Postgres (docker-compose.yml): tables and views for SQL and Metabase.
warehouse:
	PYTHONPATH=src python3 -m session_analytics warehouse --up --load --check

# Recount every session from its raw transcript and compare with what the warehouse holds.
warehouse-check:
	PYTHONPATH=src python3 -m session_analytics warehouse --check

warehouse-up:
	docker compose up -d --wait

warehouse-down:
	docker compose down

warehouse-psql:
	docker exec -it convo-analysis-pg psql -U convo -d claude_sessions

