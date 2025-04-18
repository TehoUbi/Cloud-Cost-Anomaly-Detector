# Load test results

Recorded with the profile in `tests/load/locustfile.py`.

## Setup

| | |
|---|---|
| Host | 12-core x86-64, 23 GB RAM, Linux 6.18 (WSL2) |
| Server | uvicorn, 8 workers, `src.api.main:app` |
| Database | SQLite (WAL, `synchronous=NORMAL`), 2,920 service-days across 8 services |
| Load generator | Locust `FastHttpUser`, 6 worker processes, same host |
| Profile | `DashboardUser` — read-heavy, weighted as in the locustfile |
| Duration | 60s per step, 120s at 5,000 users; spawn rate 250/s |

Two caveats worth stating plainly:

1. **The datastore is SQLite, not Postgres.** The machine these runs were done
   on has neither Docker nor a local Postgres, so the recorded numbers measure
   the API against a single-writer embedded database. Postgres would change the
   absolute figures for every uncached endpoint. The CI workflow runs the same
   profile against a Postgres service container, which is where the comparable
   numbers come from.
2. **Locust and uvicorn share 12 cores.** At 5,000 users the load generator is
   itself competing for CPU, so the latencies at that step include contention
   that would not exist with a separate driver host.

## Results

| Concurrent users | Requests | RPS | p50 | p95 | p99 | Shed (503) |
|---|---|---|---|---|---|---|
| 1,000 | 130,430 | 2,170 | 13 ms | 190 ms | 350 ms | 0.00% |
| 2,000 | 141,407 | 2,347 | 140 ms | 1,300 ms | 2,200 ms | 0.00% |
| 3,000 | 129,998 | 2,155 | 160 ms | 3,300 ms | 5,300 ms | 0.03% |
| 5,000 | 248,427 | 2,063 | 210 ms | 7,700 ms | 10,000 ms | 1.57% |

**2,000 concurrent users** is the ceiling that holds the p95 < 2s budget with
zero shed responses. At 5,000 the service still sustains ~2,060 RPS and never
returns a wrong answer, but 1.57% of requests are deliberately shed with 503
and the p95 is far outside budget.

### Per-endpoint at 5,000 users

| Endpoint | p50 | p95 | Requests | Shed |
|---|---|---|---|---|
| `/health` | 64 ms | 360 ms | 67,894 | 0.00% |
| `/costs/summary` (cached) | 42 ms | 2,200 ms | 51,272 | 0.00% |
| `/costs/services` (cached) | 42 ms | 2,000 ms | 10,290 | 0.00% |
| `/costs/baselines` (cached) | 42 ms | 1,400 ms | 6,674 | 0.00% |
| `/costs/` (paginated) | 3,900 ms | 9,600 ms | 49,277 | 3.55% |
| `/costs/?anomalies_only` | 3,900 ms | 9,500 ms | 26,424 | 3.41% |
| `/alerts/` | 3,900 ms | 9,500 ms | 19,974 | 3.35% |
| `/ready` (touches the DB) | 3,600 ms | 9,400 ms | 3,366 | 3.57% |

The split is the whole story. `/health` touches nothing and stays at 64 ms, so
the ASGI layer is not the constraint at this concurrency. Cached aggregates
stay at 42 ms. Every endpoint that runs a query per request sits at ~3.9 s.
The bottleneck is the datastore.

## What these runs changed

The first 5,000-user run reported a p95 of 6,500 ms with `/costs/summary` at
3,300 ms median. Three fixes came out of it, in order:

1. **A TTL cache on the aggregate reads** (`src/api/cache.py`). `/costs/summary`
   ran four aggregate queries over the whole window on every request, and it is
   the most-requested endpoint in the API. Cost data changes when a poll runs,
   a few times a day, so a 30-second cache is correct rather than a trick.
   Writes invalidate it. Effect: `/costs/summary` p50 3,300 ms → 42 ms, and
   aggregate p50 2,900 ms → 210 ms.

2. **A connection-pool misconfiguration**, found in the server log rather than
   the client report: `QueuePool limit of size 5 overflow 10 reached`. Those
   are SQLAlchemy's defaults, not the configured 10 + 20 — `get_engine()`
   skipped pool sizing for any URL starting with `sqlite`, on the assumption
   that the driver rejects those arguments. Only *in-memory* SQLite does;
   file-backed SQLite takes a normal queue pool. The check is now narrower.

3. **Pool exhaustion returned 500.** Once `pool_timeout` was set to shed load
   instead of blocking for 30 s, saturation surfaced as a 500, which tells a
   load balancer and a client that the service is broken and to keep hammering.
   It is now a 503 with `Retry-After`, which is what lets both back off.

The remaining ~3.9 s on uncached endpoints is SQLite write-lock serialization
and is not fixable in application code. The next thing to do is run this
against Postgres and re-measure before optimizing anything else — the
paginated endpoints each run a `COUNT(*)` alongside the page, which is the
obvious next candidate, but there is no point tuning it against the wrong
datastore.

## Reproducing

```bash
# 1. Seed data
DATABASE_URL=sqlite+aiosqlite:///./load.db COST_PROVIDER=synthetic \
  python -m scripts.seed_demo --days 365

# 2. Serve it
DATABASE_URL=sqlite+aiosqlite:///./load.db COST_PROVIDER=synthetic \
  uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --workers 8

# 3. Drive it
locust -f tests/load/locustfile.py --host http://127.0.0.1:8000 \
  --headless --users 5000 --spawn-rate 250 --run-time 120s \
  --processes 6 --csv reports/load --html reports/load.html DashboardUser
```

The run exits non-zero if the failure ratio exceeds 1% or the p95 exceeds
2,000 ms, so it is usable as a gate rather than only as a report.
