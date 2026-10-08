# GATR: a messy food manufacturer for the lab

Grupo Alimentario Tres Ríos (GATR) is a fictional mid-size food manufacturer in Argentina, and this directory is the
dataset the lab loads for it: schema `gatr` of every instance's `analytics` database. It is synthetic but built to
look like the exports of a real company's systems: several source systems, years of drift, an acquisition, manual
workarounds and human error. It is hard to analyze correctly and easy to analyze incorrectly, which is the point: it
stress-tests data engineers and analytics agents.

| | |
|---|---|
| Company | Snacks (chips, crackers), sauces (jars, sachets), frozen meals, bread. Founded 1998. |
| Plants | P1 Planta Norte (ERP since 2019, data migrated from an older system); P2 Planta Sur (opened July 2022, its own way of configuring the same ERP); P3 Planta Delta (bought March 2024, on its legacy system until a partial migration in May 2025). |
| Span | 2022-01-01 to 2026-09-30. |
| Sales | Supermarket chains and their stores, distributors, wholesalers, food service, exports (USD); domestic sales in ARS through high inflation. |
| Size | 44 tables, 7.6 million rows: 3.4 M sensor readings, 760 k batch-consumption rows, 690 k shipment lines, 660 k time-clock punches, 630 k sales order lines, 322 k inventory snapshot rows, 244 k quality results, 91 k batches; 401 products over time, 180 supplier records (164 companies), 2,223 customers (plus Planta Delta's 400), 1,587 employees. 64 MB gzipped. |

## What's where

| Path | What it is | Who should see it |
|---|---|---|
| [`../db/gatr/`](../db/gatr/) | The deliverable: one gzipped CSV per table, as the systems exported it (the mess is kept). It is part of the lab's Postgres build context. | Everyone |
| [`../db/gatr.sql`](../db/gatr.sql) | Loads them into schema `gatr`: column types a generic loader would infer (anything that doesn't parse everywhere stays text), primary keys only where the export has them clean, no foreign keys, and the data dictionary as table and column comments. | Everyone |
| [`docs/data_dictionary.md`](docs/data_dictionary.md) | The "official" documentation a new hire would get. Plausibly incomplete and partly out of date, on purpose. | Everyone |
| [`load.sh`](load.sh) | Loads GATR into a lab instance that is already running (`make gatr-load INSTANCE=<name>`), keeping its Metabase content. | Everyone |
| [`answer_key/`](answer_key/) | The planted stories, every corruption rule, the true metrics against what a naive analysis gets, the clean pre-corruption tables, and the verification report. | **Testers only** |
| [`generator/`](generator/) | The deterministic generator (one seed) and the verifier. | Testers |

**Keep agents under test away from this repository**: run them in another directory, against a lab instance's
Metabase or Postgres only. Everything they need is in the instance; the answers are here.

## Regenerating

```bash
make gatr          # ~20 minutes, ~4 GB of memory: rewrites ../db/gatr/, ../db/gatr.sql, docs/ and answer_key/
make gatr-verify   # loads the result into a throwaway Postgres (Docker) and rewrites answer_key/verification.md,
                   # ground_truth_metrics.csv and data_profile.json; GATR_PG_DSN=postgresql://... to use your own
```

Both run through `uv` with pinned versions (Python 3.13, numpy 2.5.3, pandas 3.0.5, psycopg 3.2.10). The generator is
deterministic: every random draw comes from a stream derived from one seed (`SEED` in `generator/gatr/common.py`) and
the name of the part of the world it belongs to, so changing one part doesn't reshuffle the others, and the gzipped
files carry no timestamps. The same code, seed and library versions give the same bytes (two runs from scratch were
compared file by file; on another CPU architecture, floating-point differences in the last digit are possible), so
the lab's image tag (a hash of `../db/`) only changes when the data does (`generate.py` pins `PYTHONHASHSEED`, since set iteration order
feeds the scheduler). Regenerate only when you mean to change the data: every regeneration that changes it adds up to
~135 MB (64 MB of data, 71 MB of true world) to the repository's history.

`python3 generator/generate.py --cache /tmp/gatr` pickles each stage of the true world, so iterating on a late stage
(the corruption layer, say) doesn't re-run the simulation; delete the cache after changing an early stage.

## How it is built

1. **The true world** (`generator/gatr/master.py`, `demand.py`, `ops.py`, `supply.py`, `quality.py`,
   `facilities.py`, `inventory.py`, `p3.py`): a simulation of the business as it happened. Customers order with
   seasonality, segment trends and inflation-indexed prices; a weekly make-to-stock plan schedules batches on each line
   with their real downtime, speed losses and yields; lots are released and shipped first-in first-out; raw materials
   are bought ahead of use, received into lots and consumed by batches first-in first-out; quality tests, complaints,
   credit notes, maintenance, shifts, energy and freezer temperatures follow from what happened. It is internally
   consistent: every shipped lot was produced, every consumed raw-material lot was received, mass roughly balances
   (inputs per recipe, outputs per yield), inventory reconciles to receipts minus consumption.
2. **The stories** are planted in that world as causes, not as noise: a contaminated supplier lot really goes into
   batches that really ship; a recipe change really raises failures; a freezer really warms up at night. See
   `answer_key/hidden_stories.md`.
3. **The recorded layer** (`rec_master.py`, `rec_ops.py`, `rec_sales.py`) turns the true world into what each system
   recorded. Every corruption is tied to a system, plant, person, period or process (a plant's local configuration, an
   export path, an ERP upgrade, a migration, a clerk), and logged with its counts in `answer_key/corruptions.md`.
4. **Export** (`export.py`): the recorded tables to `../db/gatr/`, their load script, the dictionary
   (`dictionary.py`), the true world to `answer_key/true_world/`.
5. **Verification** (`verify.py`): see `answer_key/verification.md`.

## Decisions

The brief left these open; this is what the generator does.

- **One schema, `gatr`, in the lab's `analytics` database**, next to `dba` and `flight_delays`, like every other lab
  dataset. Table names are the brief's, P3's legacy tables keep Spanish names and columns.
- **CSV, gzipped**, in the lab's Docker build context (`../db/`), so the image bakes them in like the other datasets:
  no download at build time. NULL is an unquoted empty field and an empty string is `""`, so Postgres keeps the
  difference. The answer key lives outside the build context, so it can never end up in an instance.
- **Types are inferred, not designed**: a column is integer, numeric, date, timestamp or boolean only when every value
  in the file parses as one, the way a generic loader (or Metabase's CSV upload) would decide. Where a source wrote
  `DD/MM/YYYY`, decimal commas, units in the value or mixed formats, the column is text. One consequence is
  deliberate: P3's legacy sales quantities use dot thousands separators, every value happens to parse as a number, so
  `1.234` units are loaded as 1.234.
- **No foreign keys**, and primary keys only where the exported key is unique and non-null: the brief asks for tables
  with surrogate keys, natural keys and none, and exports don't carry constraints.
- **The data dictionary is the comments** Metabase shows: written as GATR's IT would have in 2023, it knows nothing
  of Planta Delta, documents status codes from before the 2024 upgrade, and says rated capacity is in units per hour.
- **The span ends 2026-09-30**, a week before the data was generated, so "the last complete month" is September 2026.
- **Volumes**: 91 k recorded batches (101 k in the true world: P3 before its migration reports daily totals only),
  633 k sales order lines, 3.4 M sensor readings, 2,223 customers (plus Planta Delta's 400), 1,587 employees, 401
  products over time (393 with ERP codes, among them retired, re-coded, promotional, private-label and export SKUs and
  two reused codes; 8 only in Planta Delta's legacy system), 180 supplier records (164 companies). Sensor readings are
  every 5 minutes for 8 cold-storage units.
- **Exchange rates and inflation** are shaped like the period (a crawling peg, the December 2023 devaluation, a float
  from April 2025) but are not the official series.
- **Fictional names only**: companies and brands are generated from generic Spanish words, people from common first
  and last names; tax ids are CUIT-shaped with a deliberately wrong check digit, so none can be valid; places are real
  cities and provinces, never addresses. The verifier scans every value for ~90 real food brands, manufacturers and
  retailers.
- **Spanish where the business would use it** (product names, reasons, P3's tables, the time clock vendor's columns),
  English for the ERP's schema, as many Argentine companies' ERPs are configured.
