FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Lambda entrypoint (override with uvicorn for local dev via docker-compose)
CMD ["python", "-m", "infra.lambda_handler"]
