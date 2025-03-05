from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_db

router = APIRouter()


@router.get("/")
async def list_costs(
    service: str | None = Query(None, description="Filter by AWS service name"),
    days: int = Query(30, description="Number of days to look back"),
    db: AsyncSession = Depends(get_db),
):
    """Return historical cost records, optionally filtered by service."""
    # TODO: Query CostRecord table with filters
    raise NotImplementedError


@router.post("/poll")
async def trigger_poll(db: AsyncSession = Depends(get_db)):
    """Manually trigger a Cost Explorer poll and anomaly detection run."""
    # TODO: Call AWS Cost Explorer client, run detector, persist results
    raise NotImplementedError


@router.get("/{service}/baseline")
async def get_baseline(service: str, db: AsyncSession = Depends(get_db)):
    """Return the current rolling baseline stats for a service."""
    # TODO: Fetch baseline from DB
    raise NotImplementedError
