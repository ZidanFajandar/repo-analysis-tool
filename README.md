# Repo Analysis Tool (RAT)

A web dashboard that ingests git repositories (zip upload or clone URL) and computes
file, directory, repository, commit-set and author metrics for exploring how a codebase
evolved, who contributed where, and which parts are the most volatile.

Single FastAPI process: REST API + SQLite index + a static dashboard
(vanilla ES modules + vendored ECharts — no npm/build step).

## Quickstart

Requirements: Python >= 3.10, git (CLI), network access for cloning.

```bash
./run.sh                 # creates .venv, installs deps, serves on http://127.0.0.1:8000
```

Open http://127.0.0.1:8000, click **Add repository** and either:

- upload a **zip** of a repo (must contain `.git`; a single wrapper folder is detected), or
- paste a **clone URL** (e.g. `https://github.com/DaveGamble/cJSON.git`) — cloned bare, full history.

Indexing runs in the background with live progress on the repo card; the dashboard
opens when ready. `PORT=9000 ./run.sh` overrides the port; `RAT_DATA_DIR=<path> ./run.sh`
moves the data directory (default `./data`, gitignored).

Sample repos to try: cJSON (955 commits, a few seconds to index), Redis (11,875), git/git (61,101).

## Features (rubric map)

| Brief requirement | Where |
| --- | --- |
| File / Directory / Repository / Commit-set / Author metrics | `backend/metrics.py`, dashboard KPIs + tables |
| Zip **and** remote-URL ingestion | `backend/ingest.py` (validated URLs → HTTP 400; bad zips → actionable error) |
| Filter by repository | repo selector; multi-repo support on the repos page |
| Filter by author | author multiselect; author metrics (n_a, λ_a, ω) for the current set/object |
| Filter by file / directory | breadcrumb drill-down, treemap click, object search |
| Filter by commit time range | date pickers + **drag/zoom on the activity timeline** (brush → range) |
| Filter by manual commit list | searchable paginated commit picker (multi-select) |
| Author merging — .mailmap | applied at index time (`%aN/%aE`) |
| Author merging — manual | Authors page: select → merge into one group; unmerge any member or whole group, instant, no reindex |
| Efficient architecture | one-pass streaming indexer with per-ancestor directory rollups; all queries are indexed SQL aggregates (no subtree scans) |
| Visualisation | activity timeline (brush), calendar heatmap, cumulative growth, treemap drill-down, top-N bars, ownership donut, per-file drawer |
| Usability / QoL | shareable URL-hash deep links, loading skeletons, empty states, toasts, dark/light theme, CSV export of any view, delete/reindex repos |

## Metric definitions (as implemented)

Per commit `h` (non-merge, `-M50%` rename detection, binaries skipped):

- File: `l+` added lines, `l-` removed lines, `δ = l+ − l-` (growth), `λ = l+ + l-` (churn).
- Directory: sums over immediate children (files + subdirectories), rolled up to the root `""`.
- Repository: the root directory's metrics.
- Deleted objects are recorded as removals on their path; pure renames change nothing;
  rename+edit is attributed to the new path. Empty commits contribute no rows.
- Commit set `H` (modes): `all` (H̄ = non-merge commits reachable from HEAD), `range`
  (H_i,j: `i ≤ committer-date < j`, end exclusive — the chart brush generates this),
  `list` (explicit hashes). Per set: `n` = #commits with churn > 0 on the object,
  `η = n/|H|`, `ρ = λ/|H|` (0 when `|H| = 0`).
- Author: `n_a`, `λ_a`, ownership `ω = λ_a / λ` over the same set/object (0 when `λ = 0`).

## Architecture

```
backend/main.py      FastAPI routes, static mount, startup recovery (interrupted → failed)
backend/db.py        SQLite schema (repos, commits, file_changes, dir_changes, author_identities,
                     objects), WAL, composite indexes
backend/indexer.py   streaming `git log --root --no-merges -M50% --numstat -z` parser;
                     batched inserts; every change also writes per-ancestor dir rows
backend/ingest.py    zip extract/validate, bare clone, serialized background job with progress
backend/metrics.py   commit-set/object/author SQL aggregation (CommitFilter)
backend/authors.py   mailmap-aware groups; manual merge/unmerge
frontend/            hash-routed SPA: api.js, state.js (URL = all filter state),
                     charts.js (ECharts), views.js (pages, tables, modals, drawer)
scripts/             make_fixture.py (deterministic test repo), verify_repo.py (independent check)
tests/               fixture-based exact-value tests
```

Key ideas:

- **One-pass indexing**: a single streamed `git log` run parses headers (`\x1f` separated) and
  numstat blocks (`-z`) with an explicit state machine (rename = two tokens → new path;
  zero-churn rows and binaries dropped; empty commits handled). ~1–2k commits/s.
- **Directory rollups at write time**: each file change also writes rows for every ancestor
  directory (including `""` via `dirname`), so directory/repository metrics are exact sums
  from an index — queries never scan subtrees.
- **Reads**: everything is an indexed aggregate (`repo_id, path, committer_ts, author_id`);
  typical queries are 2–60 ms even on 61k commits.

## Performance (measured)

| Repo | Commits | Clone + index |
| --- | --- | --- |
| cJSON | 955 | a few seconds |
| redis | 11,875 | 30 s |
| git/git | 61,101 | 71 s |

Query latencies on git/git (61k commits): metrics 59 ms, tree 38 ms, timeseries 60 ms,
top-N 17–25 ms, commits page 5 ms, search 2 ms, export 20 ms — all well under a 300 ms budget.

## Correctness verification

- `tests/test_metrics.py` — 27 exact-value tests on a deterministic 8-commit fixture
  (renames, deletions, binary, empty commit, merge commit, mailmap, manual merge, H_i,j boundaries):
  ```bash
  .venv/bin/python -m pytest tests/ -q
  ```
- `scripts/verify_repo.py` — independent naive recomputation via `git show --numstat`
  cross-checked against the tool's index:
  ```bash
  RAT_DATA_DIR=data .venv/bin/python scripts/verify_repo.py <git-dir> --repo-id <id> [--path P] [--from TS --to TS]
  ```
  Verified exact matches: cJSON (full), redis H_2024 (317 commits), git/git H_2023 (2,040 commits).

### Checking provided sample metrics

Use **Commit list** mode with the sample commit hash: the commit's own diff vs its parent is
shown (`|H| = 1`), so `l+`, `l-`, `δ`, `λ` and `n` can be compared directly against the brief's
sample values.

## Notes and limitations

- Default-branch HEAD only (the brief's "typically HEAD"); branch selection is not implemented.
- "All repositories" aggregate is limited to the repos summary; metric views operate per repo.
- Binary files are excluded from metrics per the brief; rename threshold fixed at 50%.
- A deleted `.mailmap`-unmapped identity can still be merged manually on the Authors page.

## AI usage declaration

AI Declaration: This repo makes use of AI generated code (Qoder)
