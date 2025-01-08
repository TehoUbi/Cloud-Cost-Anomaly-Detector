from fastapi import FastAPI
from contextlib import asynccontextmanager

from src.api.routes import costs, alerts, health
from src.db.session import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Sentinel — Cloud Cost Anomaly Detection",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router, tags=["health"])
app.include_router(costs.router, prefix="/costs", tags=["costs"])
app.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
