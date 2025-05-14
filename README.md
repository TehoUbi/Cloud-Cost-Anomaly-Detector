# Sentinel — Cloud Cost Anomaly Detection

Polls AWS Cost Explorer, models a per-service spend baseline with a rolling
z-score detector, and delivers webhook and email alerts with exponential
backoff retry.

Runs as a FastAPI service plus a scheduled poller. The poller deploys as a
Lambda container image on an EventBridge schedule; the same cycle runs as a
long-lived process or behind `POST /costs/poll`.

```
Cost Explorer ──▶ provider ──▶ detector ──▶ PostgreSQL
                                  │              │
                          IsolationForest   pending alerts
                          (confirm/veto)         │
                                                 ▼
                                      dispatcher ──▶ webhook
                                           │     ──▶ email (SES / SMTP)
                                           └── retry, then sweep on a later poll
```

## Why it is shaped this way

The interesting parts of this are the places where the obvious implementation
does not survive contact with real cost data.

**A textbook z-score goes blind exactly when you need it.** Score against mean
and standard deviation and a single past spike inflates the baseline enough to
hide the next one. On a 30-day window poisoned with one $50k day, a genuine
$2k spike scores *z = 0.4* and nothing fires. Scoring against the median and a
MAD-derived scale puts the same point at *z = 33*. Both paths are kept and
both are tested — `tests/unit/test_detector.py` asserts that the mean/stddev
version fails on that case, because that assertion is the reason for the
design.

**Statistical significance is not the same as money.** A service billing
$0.01/day that moves to $0.08 is a 144-sigma event worth seven cents.
`MIN_ABSOLUTE_DELTA_USD` keeps it out of the alert stream.

**A spike lasts for days.** Alerting every poll for a week trains people to
filter the channel, so there is a per-service cooldown. And the day spend falls
back to normal is itself a 4-sigma drop — alerting on both directions reports
every incident twice, so drops are recorded and visible in the API but do not
page by default.

**Not every spiky series is anomalous.** A Lambda bill that genuinely triples
on release days produces a loud univariate z-score. A scikit-learn
`IsolationForest`, fitted per service on causal features of its own history,
confirms or vetoes a borderline hit. It is strictly a second opinion: a veto
only applies below 2× the threshold, anything past that goes out regardless,
and too little history makes it abstain rather than silently agree.

**An alert that fails to send is not handled by retrying five times.** If the
webhook is down for longer than one alert's retry budget, in-process retries
alone lose it. Exhausted alerts keep a `next_retry_at` and are swept on a
later poll, and every attempt is recorded with its status code and duration so
"why did this never arrive?" is answerable.

## Quick start

No AWS account needed — `COST_PROVIDER=synthetic` generates deterministic
spend with weekly seasonality, growth drift and planted spikes.

```bash
git clone https://github.com/TehoUbi/Cloud-Cost-Anomaly-Detector.git
cd Cloud-Cost-Anomaly-Detector
cp .env.example .env

python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export DATABASE_URL=sqlite+aiosqlite:///./sentinel.db
export COST_PROVIDER=synthetic

alembic upgrade head
python -m scripts.seed_demo --days 180      # ~1,440 service-days
uvicorn src.api.main:app --reload
```

Then `http://localhost:8000/docs`, or:

```bash
curl localhost:8000/costs/summary?days=90 | jq
curl 'localhost:8000/costs/?anomalies_only=true&days=90' | jq
curl localhost:8000/costs/AWS%20Lambda/baseline | jq
```

### Docker

```bash
cp .env.example .env
docker compose up --build
```

Brings up the API on `:8000`, PostgreSQL on `:5432`, and MailHog on `:8025` so
email delivery can be watched end to end without a verified SES identity.
Migrations run on start.

### Against a real account

Set `COST_PROVIDER=aws` and give the caller `ce:GetCostAndUsage`. A new
deployment has no baseline, so seed one without alerting on three months of
history:

```bash
python -m scripts.poll_and_detect --backfill 180
python -m scripts.poll_and_detect --once
```

Cost Explorer charges $0.01 per request, which is worth knowing before
pointing a polling loop at it.

## API

| | |
|---|---|
| `GET /health` | Liveness. Touches nothing. |
| `GET /ready` | Readiness. 503 if the database is unreachable. |
| `GET /costs/` | Cost history: service, date range, anomalies-only, paginated |
| `GET /costs/summary` | Spend rollup, top services, daily series, alert counts |
| `GET /costs/services` | Services with recorded spend |
| `GET /costs/{service}/baseline` | Current rolling baseline (mean, median, MAD, p95) |
| `GET /costs/{service}/history` | Chronological series for one service |
| `GET /costs/baselines` | Every baseline |
| `POST /costs/poll` | Run a cycle now |
| `POST /costs/poll/async` | Queue one, return 202 |
| `GET /alerts/` | Alerts by status, service, delivery state |
| `GET /alerts/{alert_id}` | One alert with its full per-attempt delivery log |
| `POST /alerts/{alert_id}/retry` | Retry now, ignoring the backoff schedule |
| `POST /alerts/sweep` | Flush the retry backlog |
| `POST /alerts/test` | Fire a synthetic alert through the real channels |

`/health` and `/ready` are separate deliberately: a load balancer that
health-checks the database removes every replica the moment Postgres hiccups,
turning a degraded dependency into a total outage.

## How detection works

1. **Fetch.** `GetCostAndUsage`, daily granularity, grouped by service,
   paginated. Filtered to `RECORD_TYPE=Usage` — credits and refunds post as
   negative amounts on arbitrary days and read as a spend cliff. `TimePeriod.End`
   is exclusive, so a poll covers through yesterday; today's figure is partial
   and would score as a drop.
2. **Baseline.** A trailing `WINDOW_SIZE` window per service, from stored
   history. Median and a MAD-derived scale by default. Below
   `MIN_HISTORY_DAYS` a service is not scored at all.
3. **Score.** `z = (amount − centre) / scale`, with the point appended to the
   window only after scoring, so it never contributes to the baseline it is
   judged against. Hits below `MIN_ABSOLUTE_DELTA_USD` are dropped.
4. **Confirm.** For a hit, an `IsolationForest` fitted on the same history
   confirms, vetoes, or abstains.
5. **Persist.** Upserted on `(service, usage_date)` — Cost Explorer restates
   recent days, and insert-only duplicated every service-day on each poll.
6. **Alert.** Spikes outside the cooldown become `pending` alerts.
7. **Deliver.** Every channel, each with its own retry budget. Exhausted
   alerts get a `next_retry_at` and are swept later.

Over 720 synthetic service-days this flags 8 anomalies and raises 5 alerts,
with 3 held back by the cooldown.

### Configuration worth knowing about

| Variable | Default | |
|---|---|---|
| `ZSCORE_THRESHOLD` | `3.0` | Sigma to flag a day |
| `WINDOW_SIZE` | `30` | Trailing days in the baseline |
| `MIN_HISTORY_DAYS` | `14` | MAD tolerates 50% contamination; a shorter window holding a few spikes has no trustworthy centre |
| `MIN_ABSOLUTE_DELTA_USD` | `1.0` | Dollar floor, regardless of sigma |
| `ROBUST_ZSCORE` | `true` | Median/MAD instead of mean/stddev |
| `ENSEMBLE_ENABLED` | `true` | IsolationForest confirmation |
| `ALERT_ON_DROPS` | `false` | Whether a fall in spend pages |
| `ALERT_COOLDOWN_HOURS` | `24` | Per-service repeat suppression |
| `POLL_LOOKBACK_DAYS` | `45` | Must exceed `WINDOW_SIZE` |

Full list in [`.env.example`](.env.example), which a test keeps in sync with
`src/config.py`.

## Tests

```bash
pytest                     # 416 tests
pytest tests/unit          # no services needed
pytest tests/integration   # SQLite by default
pytest --cov=src           # 95%
```

Integration tests default to SQLite so the suite runs anywhere. Point
`TEST_DATABASE_URL` at PostgreSQL to run the same tests against the deployed
target — CI does, because SQLite is more forgiving about NUMERIC handling,
`ON CONFLICT ... RETURNING` and timezone-aware timestamps.

CI also applies and rolls back every migration and runs `alembic check`, so
ORM drift fails there rather than on deploy.

## Load testing

```bash
locust -f tests/load/locustfile.py --host http://localhost:8000 \
  --headless --users 5000 --spawn-rate 250 --run-time 2m --processes 6 \
  DashboardUser
```

Measured figures, method and caveats: [`tests/load/RESULTS.md`](tests/load/RESULTS.md).

Summary of a 12-core host, 8 uvicorn workers, SQLite backend:

| Concurrent users | RPS | p50 | p95 | Shed (503) |
|---|---|---|---|---|
| 1,000 | 2,170 | 13 ms | 190 ms | 0.00% |
| 2,000 | 2,347 | 140 ms | 1,300 ms | 0.00% |
| 3,000 | 2,155 | 160 ms | 3,300 ms | 0.03% |
| 5,000 | 2,063 | 210 ms | 7,700 ms | 1.57% |

2,000 concurrent users is the ceiling that holds a p95 under 2s with nothing
shed. At 5,000 the service sustains ~2,060 RPS and never returns a wrong
answer, but 1.57% of requests are deliberately shed with 503.

Three fixes came out of those runs, in order: a TTL cache on the aggregate
reads (`/costs/summary` p50 3,300 ms → 42 ms), a connection pool silently
running on SQLAlchemy's 5+10 default instead of the configured 10+20, and pool
exhaustion returning 500 where it should return 503 with `Retry-After`. The
remaining latency on uncached endpoints is SQLite write-lock serialization,
which is not an application fix — `/health` stayed at 64 ms throughout, so the
ASGI layer is not the constraint.

## Deployment

The poller runs as a Lambda container image — numpy, scikit-learn and asyncpg
together are past the 250 MB unzipped limit for a layer-based deploy.

```bash
docker build -f infra/Dockerfile -t sentinel-poller .
docker tag sentinel-poller:latest "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/sentinel-poller:1.0.0"
docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/sentinel-poller:1.0.0"

cd infra/terraform
cp terraform.tfvars.example prod.tfvars   # then edit
terraform init && terraform apply -var-file=prod.tfvars
```

[`infra/terraform`](infra/terraform) provisions the ECR repository, execution
role, function, schedules and alarms. It deliberately does not own the VPC or
the RDS instance; those are inputs.

Two schedules: the full poll every 6h, and an hourly `{"action":"sweep"}` that
only flushes the alert retry backlog, so a webhook outage clears within the
hour. `reserved_concurrent_executions = 1`, because two concurrent polls read
the same window and race on the upsert.

One alarm watches for *absent* invocations with `treat_missing_data = "breaching"`.
A detector that has silently stopped running looks exactly like an account
with no cost anomalies.

## Project layout

```
src/
  api/          FastAPI app, routes, schemas, read cache
  detector/     zscore.py (verdicts) · ensemble.py (second opinion) · service.py (DB + loop)
  notifier/     payload.py (rendering) · channels.py (one attempt) · dispatcher.py (retries)
  utils/        cost_explorer.py (AWS + synthetic) · backoff.py
  db/           models, repositories, session
  pipeline.py   fetch → detect → deliver → sweep
migrations/     Alembic revisions
infra/          Lambda image, handler, Terraform
scripts/        poller CLI, demo seeder
tests/          unit · integration · load
```

Detection is split three ways on purpose: `zscore.py` decides whether a number
is unusual and touches no database, `ensemble.py` offers a second opinion, and
`service.py` owns persistence and the per-service loop. Delivery is separate
again — detection writes `pending` rows and returns, which is what lets a
failed send be retried on a later poll instead of lost.

## License

MIT
