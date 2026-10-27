# AGENTS.md — NHS Dentist Intelligence Platform

The canonical brief for this repository. Tool-neutral: any agent or person
should be able to work here from this file alone.

**This repository is public.** Nothing secret belongs in it.

## What this platform is

An open record of NHS dental provision in England: nightly availability
snapshots, roughly ten years of NHSBSA activity history, and a supply-versus-
demand view of where dental deserts actually are.

**Not affiliated with, endorsed by, or connected to the NHS.**

The nhs.uk directory reports what a practice claimed, not when it claimed it,
and keeps no history. Practices operate under a 90-day update mandate that is
widely unmet. This project records what was reported and when, keeps every
snapshot, and publishes its own error rates. Availability history exists
nowhere else and accrues only in wall-clock time — which is why the crawler
runs nightly regardless of what else is being built.

## Repo layout

| Path | What it holds |
|---|---|
| `ingest/` | Fetch, parse, ODS register, geocoding, entity resolution, snapshot writing, health assertions |
| `analysis/` | Supply-vs-demand (`desert.py`), freshness scoring, the static build (`build.py`) |
| `data/snapshots/YYYY-MM-DD/` | Append-only raw captures, committed to the repo. ~14,000 files |
| `data/dist/` | Generated static build outputs |
| `web/` | Next.js + MapLibre GL front end, reading the generated files |
| `tests/` | pytest suite, one module per ingest/analysis module |
| `tools/` | Operational scripts, not part of the pipeline |
| `docs/` | Architecture decision records (ADR-001 … ADR-004) |
| `CLAUDE_PROGRESS.md` | Live status and history. **Read it first on resume**; do not duplicate it here |
| `Documenting the Journey/` | The delivery ledger and build plan |

**Capture raw, parse later.** The crawler stores unparsed payloads. Yesterday
cannot be re-fetched, but it can always be re-parsed.

There is no server database. Pipelines run offline in Python and emit static
JSON and Parquet; the web app reads those files.

## Fresh clone → running

Requires **Python 3.12** (`.python-version`) and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/learndca/nhs-dentist-intelligence.git
cd nhs-dentist-intelligence
uv sync --all-extras
```

Run a capture (writes into `data/snapshots/`; takes ~2 hours for the full estate):

```bash
uv run python ingest/fetch.py --route auto
uv run python ingest/health.py     # assert the capture is healthy
```

The front end is a separate Next.js app in `web/`:

```bash
cd web && npm install && npm run dev
```

`predev`/`prebuild` run `scripts/sync-data.mjs` first, so the app needs the
generated data to exist.

## How to test

```bash
uv run pytest -q
```

198 tests, ~5 seconds. `pythonpath` is set in `pyproject.toml`, so no
`PYTHONPATH` juggling is needed.

## How to lint

```bash
uv run ruff check .
```

Line length 100, target `py312`.

## The nightly snapshot contract

`.github/workflows/snapshot.yml` — **live automation, do not break it.**

- **Schedule:** `0 3 * * *` (03:00 UTC). GitHub runs scheduled jobs late under load; actual starts have been 03:40–05:15.
- **Where:** GitHub-hosted runners. It does not depend on any developer machine being switched on.
- **Rate:** ~1 request/second, deliberately gentle. Daily, not hourly — the source operates on a 90-day mandate, so polling harder is waste and reads as abusive.
- **Credentials:** **none required.** The repo has no secrets set.
- **What it commits:** that day's `data/snapshots/YYYY-MM-DD/` back to `main`, as `github-actions[bot]`.
- **Health gate:** `ingest/health.py` fails the job if the capture is unhealthy. A run that "succeeds" with 400 practices instead of ~9,000 is worse than one that crashes, because nothing alerts.
- **Commit-on-failure is deliberate:** partial history beats none; the failed job raises the alarm.

A missed night is a permanent hole. Never disable, rename or restructure this
workflow as a side effect of other work.

## Configuration

See `.env.example`. In short: **no variable is required to run or test.**

- `NHS_API_KEY` — named in `snapshot.yml` but **read by no code in this repo**. The API route is not implemented (see ADR-002); `ingest/fetch.py --route api` exits with an error. Left in place for when it is.
- `GITHUB_TOKEN` — used only by `tools/push_via_python.py`, an operational workaround script, not the pipeline. See ADR-003.

## Conventions

- **Architecture decisions live in ADRs.** Read `docs/ADR-00*.md` before changing fetch strategy, publishing or architecture. Add a new ADR rather than editing an accepted one.
- Practice status is always presented as *"reported X on DATE"*, never as current fact.
- Every data source is OGL/Crown-copyright or ODbL; keep the licence table in the README accurate.
- British English.

## Current focus

Pre-v1, in active build. `CLAUDE_PROGRESS.md` is the live status.

## Do not

- Do not modify or disable `.github/workflows/snapshot.yml` — see the contract above.
- Do not present reported practice status as current fact.
- Do not rewrite or prune `data/snapshots/` — it is append-only and unrecoverable.
- Do not add a server database; the static-file architecture is deliberate (ADR-001).
- Do not implement the API route without a key and an ADR superseding ADR-002.
- Do not restate the ADRs or `CLAUDE_PROGRESS.md` here; reference them.
