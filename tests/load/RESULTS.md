# Load test results

Measured with `tests/load/locustfile.py`. PostgreSQL numbers are the ones that
matter; the SQLite column is kept because the first runs were done against it
and the comparison is the clearest evidence of where the bottleneck sits.

## Setup

| | |
|---|---|
| Host | 12-core x86-64, 23 GB RAM, Linux 6.18 (WSL2) |
| Server | uvicorn, 8 workers |
| PostgreSQL | 16.15 in Docker, `DB_POOL_SIZE=8`, `DB_MAX_OVERFLOW=2` |
| SQLite | WAL, `synchronous=NORMAL` |
| Data | 2,920 service-days across 8 services (one year) |
| Driver | Locust `FastHttpUser`, 4-6 worker processes, same host |
| Profile | `DashboardUser`, read-heavy, weighted as in the locustfile |
| Duration | 60s per step, 120s at 5,000 users; spawn rate 250/s |

The pool is sized at 8 + 2 because PostgreSQL's default `max_connections` is
100 and the pool is **per worker** — 8 workers at the shipped defaults of
10 + 20 would try to open 240 connections and fall over. Size the pool to the
database, or put PgBouncer in front.

Locust and uvicorn share the same 12 cores, so the 5,000-user step includes
CPU contention from the load generator that would not exist with a separate
driver host. Treat the shape as real and the absolute numbers as pessimistic.

## Results

| Users | Backend | Requests | RPS | p50 | p95 | p99 | Shed (503) |
|---|---|---|---|---|---|---|---|
| 1,000 | PostgreSQL | 139,551 | 2,323 | 6 ms | 62 ms | 130 ms | 0.00% |
| 1,000 | SQLite | 130,430 | 2,170 | 13 ms | 190 ms | 350 ms | 0.00% |
| 2,000 | PostgreSQL | 170,440 | 2,829 | 100 ms | 850 ms | 1,200 ms | 0.00% |
| 2,000 | SQLite | 141,407 | 2,347 | 140 ms | 1,300 ms | 2,200 ms | 0.00% |
| 3,000 | PostgreSQL | 162,964 | 2,701 | 120 ms | 1,800 ms | 2,900 ms | 0.00% |
| 3,000 | SQLite | 129,998 | 2,155 | 160 ms | 3,300 ms | 5,300 ms | 0.03% |
| 5,000 | PostgreSQL | 316,484 | 2,628 | 190 ms | 3,600 ms | 6,400 ms | 0.04% |
| 5,000 | SQLite | 248,427 | 2,063 | 210 ms | 7,700 ms | 10,000 ms | 1.57% |

On PostgreSQL, **3,000 concurrent users** is the ceiling that holds the
p95 < 2s budget with nothing shed. At 5,000 the service sustains ~2,630 RPS
with 0.04% of requests shed as 503 and no incorrect responses, but the p95 is
well outside budget.

Swapping SQLite for PostgreSQL moved the passing ceiling from 2,000 to 3,000
users and cut the shed rate at 5,000 users from 1.57% to 0.04%.

### Per-endpoint, PostgreSQL, 5,000 users

| Endpoint | p50 | p95 | Requests | Shed |
|---|---|---|---|---|
| `/costs/summary` (cached) | 30 ms | 990 ms | 65,295 | 0.00% |
| `/costs/services` (cached) | 30 ms | 790 ms | 12,913 | 0.00% |
| `/costs/baselines` (cached) | 30 ms | 1,100 ms | 8,797 | 0.00% |
| `/health` (no I/O) | 44 ms | 300 ms | 86,661 | 0.00% |
| `/ready` (touches the DB) | 2,100 ms | 3,800 ms | 4,254 | 0.00% |
| `/costs/` (paginated) | 2,900 ms | 5,400 ms | 62,855 | 0.10% |
| `/costs/?anomalies_only` | 2,900 ms | 5,400 ms | 33,779 | 0.09% |
| `/alerts/` | 2,900 ms | 5,400 ms | 25,030 | 0.08% |

That split is the whole story, and it is the same split on both backends:
anything served from cache or from no I/O at all stays at 30-44 ms, and
everything that takes a connection per request sits around 2.9 s. At this
concurrency the constraint is connection capacity, not query cost — the next
lever is replicas or PgBouncer, not an index.

## What these runs changed

The first 5,000-user run reported a p95 of 6,500 ms with `/costs/summary` at
a 3,300 ms median, while `/health` stayed at 280 ms. Three fixes came out of
it, in order:

1. **A TTL cache on the aggregate reads** (`src/api/cache.py`).
   `/costs/summary` ran four aggregate queries over the whole window on every
   request and is the most-requested endpoint in the profile. Cost data changes
   when a poll runs — a few times a day — so a 30-second cache is correct
   rather than a trick, and writes invalidate it. Recomputation is
   single-flight per key, because otherwise every request arriving at the
   moment an entry expires runs the same aggregate query, which is a worse
   spike than having no cache. `/costs/summary` p50 went 3,300 ms → 42 ms.

2. **A connection-pool misconfiguration**, found in the server log rather than
   the client report: `QueuePool limit of size 5 overflow 10 reached`. Those
   are SQLAlchemy's defaults, not the configured 10 + 20 — `get_engine()`
   skipped pool sizing for any URL starting with `sqlite`, on the assumption
   that the driver rejects those arguments. Only *in-memory* SQLite does;
   file-backed SQLite gets an ordinary queue pool and accepts them.

3. **Pool exhaustion returned 500.** Once `pool_timeout` was set so that
   saturation sheds load instead of blocking for 30 s, it surfaced as a 500,
   which tells a load balancer and a client that the service is broken and to
   keep hammering. It is now a 503 with `Retry-After`.

## Reproducing

```bash
# PostgreSQL
docker compose up -d db
export DATABASE_URL=postgresql+asyncpg://sentinel:sentinel@127.0.0.1:5432/sentinel
export COST_PROVIDER=synthetic DB_POOL_SIZE=8 DB_MAX_OVERFLOW=2

alembic upgrade head
python -m scripts.seed_demo --days 365
uvicorn src.api.main:app --host 127.0.0.1 --port 8100 --workers 8

# Drive it
locust -f tests/load/locustfile.py --host http://127.0.0.1:8100 \
  --headless --users 5000 --spawn-rate 250 --run-time 120s \
  --processes 6 --csv reports/load --html reports/load.html DashboardUser
```

The run exits non-zero if the failure ratio exceeds 1% or the p95 exceeds
2,000 ms, so it works as a gate and not only as a report.
