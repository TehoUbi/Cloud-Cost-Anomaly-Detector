# Sentinel

Sentinel tracks daily AWS costs and flags unusual spend for each service. The
dashboard shows cost trends, the largest services, and the days that differ from
their baseline. Alerts can go to a webhook, email, or both.

It runs as a FastAPI app with a separate poller. Use the synthetic data provider
to try it locally, or connect AWS Cost Explorer to monitor an account.

## Run locally

Requires Python 3.12 or later. This setup uses SQLite and generated data; no AWS
account is needed.

```bash
git clone https://github.com/TehoUbi/Cloud-Cost-Anomaly-Detector.git
cd Cloud-Cost-Anomaly-Detector
cp .env.example .env

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

export DATABASE_URL=sqlite+aiosqlite:///./sentinel.db
export COST_PROVIDER=synthetic

alembic upgrade head
python -m scripts.seed_demo --days 180
uvicorn src.api.main:app --reload
```

Open [localhost:8000](http://localhost:8000) for the dashboard or
[/docs](http://localhost:8000/docs) for the API reference. The dashboard is served
by FastAPI and needs no Node.js build step or external asset service.

Select a 7, 30, or 90 day window, inspect daily spend in the chart, or filter the
anomaly table by service. Open a row's details to see the expected spend,
deviation, and detector. Spend and anomalies use the selected period; tracked
services and alert delivery counts cover all stored history. **Refresh** reloads
stored data. It does not run a poll or send alerts.

The demo seeder creates repeatable service costs with weekly patterns and
occasional spikes. It records detections without delivering alerts. The dashboard
labels this data as synthetic.

### Docker

```bash
cp .env.example .env  # if you haven't already
# Set COST_PROVIDER=synthetic in .env for a demo.
docker compose up --build
```

Compose starts the app at `http://localhost:8000`, PostgreSQL on port `5432`, and
MailHog at `http://localhost:8025`. Migrations run when the app starts. To populate
the dashboard:

```bash
docker compose exec api python -m scripts.seed_demo --days 180
```

To test email delivery locally, set `ALERT_EMAIL_TRANSPORT=smtp` and fill in
`ALERT_EMAIL_FROM` and `ALERT_EMAIL_TO` in `.env`. Compose supplies the MailHog
host, port, and TLS settings.

## Connect AWS

Set `COST_PROVIDER=aws` and configure AWS credentials through your usual profile,
environment variables, or execution role. The caller needs `ce:GetCostAndUsage`.
Use PostgreSQL for a deployed instance.

Backfill history before the first live poll so the detector has a baseline:

```bash
python -m scripts.poll_and_detect --backfill 180
python -m scripts.poll_and_detect --once
```

Backfill stores and scores history without delivering alerts. For a long-running
poller, omit `--once`:

```bash
python -m scripts.poll_and_detect
```

The default interval is six hours. Cost Explorer returns daily data through
yesterday; today's incomplete spend is excluded. Polls upsert existing service-day
records because AWS can revise recent costs. Cost Explorer requests can incur AWS
charges.

## Detection

Each service is scored against its own trailing window. By default, the detector
uses a median and a scale derived from the median absolute deviation (MAD). This
keeps an earlier spike from inflating the baseline enough to hide a later one.
A day is scored before it is added to that window.

A z-score hit is checked by an `IsolationForest` trained on the service's history.
It can veto borderline hits, but scores at or above twice the configured threshold
bypass that veto. With too little history, the forest abstains. Services with fewer
than `MIN_HISTORY_DAYS` observations are not scored.

The dollar floor filters out statistically unusual changes that cost very little.
Drops remain visible in cost history, but do not send alerts by default. A
per-service cooldown limits repeated notifications during a sustained spike.

| Setting | Default | Purpose |
| --- | --- | --- |
| `ZSCORE_THRESHOLD` | `3.0` | Minimum absolute z-score for an anomaly |
| `WINDOW_SIZE` | `30` | Observations in the trailing baseline |
| `MIN_HISTORY_DAYS` | `14` | Minimum history before scoring |
| `MIN_ABSOLUTE_DELTA_USD` | `1.0` | Minimum change in dollars |
| `ROBUST_ZSCORE` | `true` | Use median/MAD rather than mean/stddev |
| `ENSEMBLE_ENABLED` | `true` | Check hits with IsolationForest |
| `ALERT_ON_DROPS` | `false` | Send alerts for decreases in spend |
| `ALERT_COOLDOWN_HOURS` | `24` | Suppress repeat alerts per service |
| `POLL_LOOKBACK_DAYS` | `45` | Days fetched per poll; keep above `WINDOW_SIZE` |

See [`.env.example`](.env.example) for database, polling, and delivery settings.

## Alert delivery

Set `ALERT_WEBHOOK_URL` for webhooks. For email, choose `ses` or `smtp` with
`ALERT_EMAIL_TRANSPORT` and set the sender and recipients. SES also requires a
verified identity and the appropriate AWS permissions.

Each configured channel has its own retry budget with exponential backoff and
jitter. Transient failures such as timeouts, rate limits, and server errors are
retried. Permanent failures, such as an invalid recipient or malformed webhook
request, stop the current dispatch attempt.

Alerts that exhaust their retries remain in the database with a next retry time.
Later polls sweep the backlog. Every delivery attempt records its outcome, status
code, and duration; `GET /alerts/{alert_id}` returns that log.

`POST /alerts/test` sends a synthetic alert through the configured channels
without storing it. Use it to check delivery after configuring a channel.

## API

The interactive reference at `/docs` includes request schemas and query parameters.

| Endpoint | Purpose |
| --- | --- |
| `GET /` | Dashboard |
| `GET /health` | Liveness, without querying the database |
| `GET /ready` | Database readiness and provider configuration |
| `GET /costs/` | Paginated cost records; filter by service, dates, or anomalies |
| `GET /costs/summary` | Spend totals, daily series, top services, and alert counts |
| `GET /costs/services` | Service names with stored history |
| `GET /costs/baselines` | Current service baselines |
| `GET /costs/{service}/baseline` | Baseline for one service |
| `GET /costs/{service}/history` | Chronological service history |
| `POST /costs/poll` | Run a poll and wait for its result |
| `POST /costs/poll/async` | Queue a poll and return `202` |
| `GET /alerts/` | Paginated alerts with status, service, and delivery filters |
| `GET /alerts/{alert_id}` | Alert details and delivery attempts |
| `POST /alerts/{alert_id}/retry` | Retry an alert immediately |
| `POST /alerts/sweep` | Process due retries |
| `POST /alerts/test` | Send a test alert |

For example:

```bash
curl 'localhost:8000/costs/summary?days=90'
curl 'localhost:8000/costs/?anomalies_only=true&days=90'
curl 'localhost:8000/costs/AWS%20Lambda/baseline'
```

Aggregate reads use a 30-second cache. Polls and alert writes invalidate it.
Database pool saturation returns `503` with `Retry-After`.

## Development

```bash
pytest
pytest tests/unit
pytest --cov=src
ruff check src tests infra scripts migrations
ruff format --check src tests infra scripts
mypy src
```

Integration tests use a temporary SQLite database by default. Set
`TEST_DATABASE_URL` to run them against PostgreSQL. CI runs both unit and
PostgreSQL integration tests, checks migration upgrades and rollbacks, and builds
the API and Lambda images.

Load-test results and their conditions are in
[`tests/load/RESULTS.md`](tests/load/RESULTS.md). Database pools are per worker;
size `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` to fit the database connection limit.

## Deployment

The API image uses the root [Dockerfile](Dockerfile). The scheduled poller uses
[`infra/Dockerfile`](infra/Dockerfile), which bundles the Python and ML dependencies
in a Lambda container image.

[`infra/terraform`](infra/terraform) defines the ECR repository, Lambda execution
role, function, EventBridge schedules, and alarms. Supply your existing VPC and
RDS settings through its variables. See
[`terraform.tfvars.example`](infra/terraform/terraform.tfvars.example).

The full poll runs every six hours. A separate hourly invocation sweeps pending
alert retries. Lambda concurrency is limited to one to avoid overlapping polls.
The API is deployed separately from the poller.

## Project layout

```text
src/
  api/          FastAPI routes, schemas, cache, and dashboard assets
  detector/     Statistical scoring, ensemble, and detection service
  notifier/     Payloads, delivery channels, and retry dispatcher
  utils/        AWS and synthetic providers, backoff
  db/           Sessions and repositories
  models/       Database models
  pipeline.py   Fetch, detect, deliver, and sweep
migrations/     Alembic revisions
infra/          Lambda handler, container image, and Terraform
scripts/        Poller CLI and demo seeder
tests/          Unit, integration, and load tests
```

## License

MIT
