# Sentinel

Watches your AWS bill and tells you when a service starts costing more than it
should.

It polls Cost Explorer for daily per-service spend, keeps a rolling baseline
for each service, and when today's number is far enough outside that baseline
it sends you a webhook and an email. There's a FastAPI service for querying the
history and a scheduled poller that does the actual work. The poller runs on
Lambda as a container image, but it'll also run as a plain long-lived process
or on demand via `POST /costs/poll`.

```
Cost Explorer → provider → detector → PostgreSQL
                              ↓           ↓
                     IsolationForest   pending alerts
                     (confirm/veto)       ↓
                                      dispatcher → webhook
                                                 → email (SES or SMTP)
                                                 → retry, then sweep later
```

## Try it

You don't need an AWS account. `COST_PROVIDER=synthetic` generates believable
spend data (weekly seasonality, slow growth, occasional planted spikes) and
it's seeded per service-day, so you get the same numbers every run.

```bash
git clone https://github.com/TehoUbi/Cloud-Cost-Anomaly-Detector.git
cd Cloud-Cost-Anomaly-Detector
cp .env.example .env

python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

export DATABASE_URL=sqlite+aiosqlite:///./sentinel.db
export COST_PROVIDER=synthetic

alembic upgrade head
python -m scripts.seed_demo --days 180
uvicorn src.api.main:app --reload
```

Open http://localhost:8000/docs, or poke at it directly:

```bash
curl 'localhost:8000/costs/summary?days=90' | jq
curl 'localhost:8000/costs/?anomalies_only=true&days=90' | jq
curl 'localhost:8000/costs/AWS%20Lambda/baseline' | jq
```

If you'd rather use Docker, `docker compose up --build` gets you the API on
`:8000`, Postgres on `:5432`, and MailHog on `:8025`. Migrations run on
startup. MailHog is there so you can watch the alert emails actually arrive
without setting up a verified SES identity, which saves a lot of guessing
when you're changing the email template.

### Pointing it at a real account

Set `COST_PROVIDER=aws` and make sure the caller has `ce:GetCostAndUsage`.

A fresh install has no history, so every service's first couple of weeks get
judged against a window that's still filling up. Seed it first and don't alert
on any of it:

```bash
python -m scripts.poll_and_detect --backfill 180
python -m scripts.poll_and_detect --once
```

Worth knowing: Cost Explorer bills a cent per request. That's nothing in
production at one poll every six hours, but it adds up fast if you leave a
dev loop pointed at it.

## The detector

This is the part that took the most iterating, so it's worth explaining why
it ended up the way it did.

The obvious implementation is a z-score against the mean and standard
deviation of the last 30 days. That has a bad failure mode: one expensive day
in the window inflates the standard deviation enough to hide the next one. I
hit this on generated data with a single $50k spike in the window: a real
$2k spike afterwards scored **z = -0.01** and nothing fired at all. Scoring
against the median and a MAD-derived scale instead puts that same point at
**z = 165**. Both code paths are still there and both are tested, including a
test that asserts the mean/stddev version fails on that input, because that
assertion is the reason the robust version exists.

Then there's the problem that statistical significance and money aren't the
same thing. KMS costing a cent a day and jumping to eight cents is a
139-sigma event and seven cents of actual money. `MIN_ABSOLUTE_DELTA_USD`
stops that from waking anyone up.

Cost spikes also last for days, not hours. A forgotten NAT gateway is still
there tomorrow. Alerting on it every six hours for a week is how you teach
people to filter your alerts, so there's a per-service cooldown. Related: when
spend finally drops back to normal, that drop is itself a 4-sigma event. The
first version alerted on it and reported every incident twice. Drops are still
detected and still visible in the API, they just don't page by default.

Finally, not every spiky service is a problem. A Lambda bill that genuinely
triples on release days will set off a univariate z-score constantly. So
there's a scikit-learn `IsolationForest` fitted per service on causal features
of its own history (level, log level, ratio to the trailing median,
day-over-day change, deviation from the same weekday) that either confirms or
vetoes a borderline hit. It only gets to veto below 2× the threshold, anything
past that goes out regardless of what the forest thinks, and if there isn't
enough history it abstains rather than silently agreeing. It's a second
opinion, not the decision.

### What a cycle actually does

1. Call `GetCostAndUsage`, daily, grouped by service, following pagination.
   Filtered to `RECORD_TYPE=Usage`, because credits and refunds post as
   negative amounts on arbitrary days and look exactly like a cliff in spend.
   `TimePeriod.End` is exclusive, so a poll covers through yesterday; today is
   always partial and would score as a drop.
2. Build a trailing window per service from stored history. Services with
   fewer than `MIN_HISTORY_DAYS` don't get scored at all.
3. Score each day: `z = (amount − centre) / scale`. The point gets appended to
   the window *after* it's scored, so it never contributes to the baseline
   it's being judged against.
4. For anything that fires, ask the forest.
5. Write the row, upserting on `(service, usage_date)`. Cost Explorer restates
   recent days as usage settles, and the original insert-only version happily
   duplicated every service-day on every poll.
6. Spikes outside the cooldown become `pending` alerts.
7. Deliver them.

Over 720 synthetic service-days that comes out at 8 anomalies and 5 alerts,
with 3 held back by the cooldown.

### Settings you might actually want to change

| | default | |
|---|---|---|
| `ZSCORE_THRESHOLD` | `3.0` | sigma needed to flag a day |
| `WINDOW_SIZE` | `30` | days in the baseline |
| `MIN_HISTORY_DAYS` | `14` | MAD copes with up to 50% bad data, and a shorter window holding two or three spikes has no trustworthy middle |
| `MIN_ABSOLUTE_DELTA_USD` | `1.0` | dollar floor, whatever the sigma says |
| `ROBUST_ZSCORE` | `true` | median/MAD instead of mean/stddev |
| `ENSEMBLE_ENABLED` | `true` | the IsolationForest stage |
| `ALERT_ON_DROPS` | `false` | whether a fall in spend pages you |
| `ALERT_COOLDOWN_HOURS` | `24` | per-service repeat suppression |
| `POLL_LOOKBACK_DAYS` | `45` | has to be more than `WINDOW_SIZE` |

Everything else is in [`.env.example`](.env.example), which a test keeps in
sync with `src/config.py` so it can't quietly go stale.

## Alert delivery

Each alert goes to every channel you've configured, and each channel gets its
own retry budget: 1s, 2s, 4s, 8s, 16s with jitter, so a fleet of workers
hitting the same dead webhook don't all come back at the same instant.

Failures are classified before they're retried. A 400 is permanent and raises
immediately, because a malformed payload will be just as malformed in eight
seconds. A 408, 429, 5xx or connection error is transient and gets the ladder.
Same on the email side: an unverified SES sender or a refused recipient is
permanent, throttling isn't.

The part I'd point at is what happens when the ladder runs out. In-process
retries alone lose the alert if the webhook is down longer than one alert's
budget, which it will be. So an exhausted alert keeps a `next_retry_at` and
gets picked up by a sweep on a later poll. Every attempt is written to
`alert_attempts` with its status code and duration, which means
`GET /alerts/{id}` can actually answer "why didn't this arrive?".

There's a `POST /alerts/test` that fires a synthetic alert through the real
channels and persists nothing, for checking your webhook URL and SES setup
after a deploy.

## API

`GET /health` is liveness and touches nothing. `GET /ready` is readiness and
returns 503 if the database is unreachable. These are separate on purpose: if
your load balancer health-checks the database, a Postgres hiccup pulls every
replica out of rotation at once and turns a degraded dependency into an
outage.

Reads: `GET /costs/` (filter by service, date range, anomalies only;
paginated), `GET /costs/summary` for the rollup, `GET /costs/services`,
`GET /costs/{service}/baseline`, `GET /costs/{service}/history`,
`GET /costs/baselines`.

Alerts: `GET /alerts/` with status/service/delivery filters,
`GET /alerts/{alert_id}` for one alert plus its full attempt log,
`POST /alerts/{alert_id}/retry` to retry now and ignore the backoff schedule,
`POST /alerts/sweep` to flush the backlog, `POST /alerts/test` as above.

Writes: `POST /costs/poll` runs a cycle and waits, `POST /costs/poll/async`
queues one and hands back a 202, which you want on a large account where a
full cycle can outlive the client's timeout.

Full schema at `/docs`.

## Tests

```bash
pytest                   # 422 tests
pytest tests/unit        # no services needed
pytest --cov=src         # 95%
```

Integration tests run against SQLite by default so the suite works anywhere.
Set `TEST_DATABASE_URL` to a Postgres URL and the same tests run against the
real thing, which is what CI does, because SQLite is more forgiving than
Postgres about NUMERIC handling, `ON CONFLICT ... RETURNING` and
timezone-aware timestamps. They pass on both.

CI also applies and rolls back every migration and runs `alembic check`, so if
the ORM drifts from the revisions you find out there rather than during a
deploy.

## Performance

Numbers, method and caveats are in
[`tests/load/RESULTS.md`](tests/load/RESULTS.md). The short version, on
Postgres with 8 uvicorn workers:

| Users | RPS | p50 | p95 | Shed |
|---|---|---|---|---|
| 1,000 | 2,323 | 6 ms | 62 ms | 0.00% |
| 2,000 | 2,829 | 100 ms | 850 ms | 0.00% |
| 3,000 | 2,701 | 120 ms | 1,800 ms | 0.00% |
| 5,000 | 2,628 | 190 ms | 3,600 ms | 0.04% |

3,000 concurrent users is where it stops meeting a 2s p95. At 5,000 it still
does ~2,630 RPS and never returns a wrong answer, but 0.04% of requests get
shed with a 503.

Three things came out of those runs. The aggregate endpoints now sit behind a
30-second TTL cache, which took `/costs/summary` from a 3,300 ms median to
30 ms; the data only changes when a poll runs, so caching it isn't a cheat,
and writes invalidate it. The connection pool turned out to be running
on SQLAlchemy's 5+10 default rather than the configured 10+20, which I only
spotted because the server log said so. And once `pool_timeout` was set so
saturation shed load instead of blocking for 30 seconds, it was returning 500,
which tells your load balancer the service is broken and to keep hammering
it. It returns 503 with `Retry-After` now.

One thing to be careful about: the pool is **per worker**. Eight workers at the
default 10+20 wants 240 connections, and Postgres ships with
`max_connections=100`. Size it down or put PgBouncer in front.

## Deploying

The poller is a Lambda container image rather than a zip, because numpy,
scikit-learn and asyncpg together blow past the 250 MB unzipped limit.

```bash
docker build -f infra/Dockerfile -t sentinel-poller .
docker tag sentinel-poller:latest "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/sentinel-poller:1.0.0"
docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/sentinel-poller:1.0.0"

cd infra/terraform
cp terraform.tfvars.example prod.tfvars   # edit this
terraform init && terraform apply -var-file=prod.tfvars
```

[`infra/terraform`](infra/terraform) sets up the ECR repo, execution role,
function, schedules and alarms. It doesn't own your VPC or your RDS instance;
those come in as variables, because a cost monitor has no business being the
root module for the account it's watching.

There are two schedules. The full poll runs every six hours. A separate hourly
rule invokes `{"action": "sweep"}`, which only flushes the alert retry
backlog, so a webhook outage clears within the hour instead of waiting for the
next poll. The sweep costs about 20 ms when there's nothing to do.
`reserved_concurrent_executions = 1`, since two concurrent polls would read
the same window and race each other on the upsert.

One of the alarms watches for *absent* invocations with
`treat_missing_data = "breaching"`. A detector that has quietly stopped
running looks identical to an account with no cost anomalies, and that's the
failure I'd actually want to be woken up for.

## Layout

```
src/
  api/          app, routes, schemas, read cache
  detector/     zscore.py · ensemble.py · service.py
  notifier/     payload.py · channels.py · dispatcher.py
  utils/        cost_explorer.py (AWS + synthetic) · backoff.py
  db/           models, repositories, session
  pipeline.py   fetch → detect → deliver → sweep
migrations/     Alembic revisions
infra/          Lambda image, handler, Terraform
scripts/        poller CLI, demo seeder
tests/          unit · integration · load
```

Detection is split three ways because the pieces have different jobs. `zscore.py` decides whether a number is unusual and never touches a
database, which is what makes it pleasant to test. `ensemble.py` is the second
opinion. `service.py` owns persistence and the per-service loop.

Delivery is separate again. Detection writes `pending` rows and returns, and
the dispatcher sends them. That split is the only reason a failed send can be
retried on a later poll instead of disappearing.

## License

MIT
