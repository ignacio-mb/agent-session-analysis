# GATR answer key

**For testers only.** Everything here describes what was planted in GATR's data and what the right answers are. None
of it is in the lab's Postgres image or in `lab/db/`; keep agents under test from reading this directory.

| File | What it is |
|---|---|
| [hidden_stories.md](hidden_stories.md) | The 15 planted stories and the red herrings: what happened, the exact ids and dates, why a naive analysis misses it, and the analysis path that finds it. |
| [queries/](queries/) | One SQL query per story that finds it from the recorded tables alone (schema `gatr`, Postgres). |
| [verification.md](verification.md) | What each query returned on the shipped data, what the naive single-table query returns instead, the naive-vs-true metric gaps, the mess profile, and the real-name scan. Written by `generator/verify.py`. |
| [ground_truth_metrics.csv](ground_truth_metrics.csv) | 32 key metrics (with their dimensions: month, line, plant...) as computed from the true world, next to what a naive query on the recorded tables gives, and how that naive query was done. |
| [corruptions.md](corruptions.md) | Every corruption rule applied to turn the true world into the recorded tables: the source system or process it comes from, the tables and columns, and how many rows it touched. |
| [true_world/](true_world/) | The clean, pre-corruption world, one gzipped CSV per table: what really happened (true quantities, local times, canonical ids, every lot on every shipment, downtime as it happened, physical stock), plus truth-only columns (which receipts were inflated, which shifts' overtime was unneeded, what caused each complaint). |
| [story_facts.json](story_facts.json) | The parameters each story was planted with, as the generator recorded them. |
| [table_stats.json](table_stats.json), [data_profile.json](data_profile.json) | Per table and column: rows, inferred types, NULLs, empty strings and null-like values, duplicate rows. |

## Using it to grade an agent

Give the agent a lab instance and a question; compare its answer with `ground_truth_metrics.csv` (the `true_value`
column) and see whether it lands on the `naive_value` instead. Good questions start where a story hides:

- "Which line has the best OEE?" (story 4), "Which plant has the best yield?" (9), "Who is our largest customer?"
  (10), "How much did revenue grow since 2022?" (8), "Which customers received product from the sesame recall?" (1),
  "Is any supplier's quality getting worse?" (2), "Why are mayonnaise complaints up in late 2024?" (3), "Where are we
  losing inventory?" (5), "Is cold storage OK?" (6), "Compare the three plants' output" (7), "Is overtime under
  control?" (12), "Are we pricing food service right?" (13), "Which reps should we reward?" (14), "Is SUR-01's yield
  dip a process problem?" (15).
- The data dictionary (`../docs/data_dictionary.md`) is what the agent would be handed; it is wrong in places on
  purpose.
