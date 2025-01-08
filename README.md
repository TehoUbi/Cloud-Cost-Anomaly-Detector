#  Sentinel — Cloud Cost Anomaly Detection

A service that polls AWS Cost Explorer to model per-service spend baselines, detects cost spikes using a rolling z-score algorithm, and delivers webhook alerts via email with exponential backoff retry.

## Features

-  **Baseline Modeling** — Establishes rolling spend baselines per AWS service
-  **Z-Score Anomaly Detection** — Flags statistically significant cost spikes in real time
-  **Email Webhook Alerts** — Delivers alerts with full context and exponential backoff retry on failure
-  **Containerized** — Docker-ready for local dev and AWS Lambda deployment
-  **Persistent Storage** — PostgreSQL backend for historical cost data and alert logs

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| API Framework | FastAPI |
| Database | PostgreSQL |
| ML / Stats | scikit-learn, NumPy |
| Infrastructure | Docker, AWS Lambda, AWS Cost Explorer |
| Testing | pytest, Locust |

## Project Structure

```
sentinel/
├── src/
│   ├── api/            # FastAPI routes and request/response schemas
│   ├── detector/       # Z-score anomaly detection logic
│   ├── notifier/       # Email / webhook alert delivery with retry
│   ├── models/         # SQLAlchemy ORM models
│   ├── db/             # Database connection, migrations (Alembic)
│   └── utils/          # AWS Cost Explorer client, helpers
├── tests/
│   ├── unit/           # Unit tests for detector, notifier, utils
│   └── integration/    # Integration tests against real DB / AWS sandbox
├── infra/              # Dockerfile, Lambda handler, terraform (optional)
├── scripts/            # DB seed, backfill, manual trigger scripts
├── .env.example
├── docker-compose.yml
└── requirements.txt
```

## Getting Started

### Prerequisites

- Python 3.12+
- Docker & Docker Compose
- AWS credentials with `ce:GetCostAndUsage` permission
- A PostgreSQL instance (or use the included Docker Compose setup)

### Setup

1. Clone the repo:
   ```bash
   git clone https://github.com/TehoUbi/sentinel-cloud-cost.git
   cd sentinel-cloud-cost
   ```

2. Copy and configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your AWS credentials, DB URL, and alert webhook
   ```

3. Start the database:
   ```bash
   docker-compose up -d db
   ```

4. Install dependencies and run migrations:
   ```bash
   pip install -r requirements.txt
   alembic upgrade head
   ```

5. Run the API locally:
   ```bash
   uvicorn src.api.main:app --reload
   ```

### Docker (full stack)

```bash
docker-compose up --build
```

### Run Tests

```bash
pytest tests/unit
pytest tests/integration   # Requires .env with live DB
```

### Load Testing

```bash
locust -f tests/locustfile.py --host=http://localhost:8000
```

## Environment Variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_DEFAULT_REGION` | AWS region (e.g. `us-east-1`) |
| `ALERT_WEBHOOK_URL` | Webhook endpoint for alert delivery |
| `ALERT_EMAIL` | Recipient email for cost spike alerts |
| `ZSCORE_THRESHOLD` | Z-score threshold to flag anomaly (default: `3.0`) |
| `POLL_INTERVAL_HOURS` | How often to poll Cost Explorer (default: `6`) |

## How It Works

1. **Polling** — A scheduler calls AWS Cost Explorer on a configurable interval to fetch per-service daily spend.
2. **Baseline** — A rolling window of historical spend is maintained per service in PostgreSQL.
3. **Detection** — Each new data point is scored against the baseline using a z-score. Points exceeding `ZSCORE_THRESHOLD` are flagged.
4. **Alerting** — Flagged anomalies trigger webhook/email alerts. Failed deliveries are retried with exponential backoff.

## Deployment (AWS Lambda)

```bash
docker build -t sentinel .
# Tag and push to ECR, then deploy via Lambda container image
```

See `infra/` for a sample Lambda handler and optional Terraform config.

## Contributing

Issues and PRs welcome. Please include tests for any new detection logic.

## License

MIT
