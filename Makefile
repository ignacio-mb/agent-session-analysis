PY39 := /usr/bin/python3

.PHONY: test test39 lint smoke render-check install uninstall warehouse warehouse-check warehouse-up warehouse-down warehouse-psql \
	env clickhouse clickhouse-forget clickhouse-dev clickhouse-dev-test clickhouse-dev-down

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

# The ClickHouse connection string lives in ~/.config/convo-analysis/.env: create it from .env.example (never over
# an existing one), then fill in CLICKHOUSE_URL.
env:
	PYTHONPATH=src python3 -m session_analytics warehouse --init-env

# This machine's sessions into the shared ClickHouse (replacing only its own rows), then the check.
clickhouse:
	PYTHONPATH=src python3 -m session_analytics warehouse --clickhouse --check

# Take this machine's rows out of the shared ClickHouse; everyone else's stay.
clickhouse-forget:
	PYTHONPATH=src python3 -m session_analytics warehouse --clickhouse-forget

# A throwaway local ClickHouse (docker-compose.yml, profile clickhouse) to try the load and every card's SQL on.
clickhouse-dev:
	docker compose --profile clickhouse up -d --wait clickhouse

clickhouse-dev-test: clickhouse-dev
	@mkdir -p .preview && printf 'CLICKHOUSE_URL=http://convo:convo@127.0.0.1:18123/sessions\n' > .preview/clickhouse-dev.env
	PYTHONPATH=src python3 -m session_analytics warehouse --clickhouse --env-file .preview/clickhouse-dev.env --check \
		--out .preview/clickhouse-dev
	python3 scripts/metabase_dashboard.py --test --clickhouse --env-file .preview/clickhouse-dev.env

# Only the throwaway ClickHouse: `down` would take the Postgres warehouse's container with it.
clickhouse-dev-down:
	docker compose --profile clickhouse rm --stop --force clickhouse
