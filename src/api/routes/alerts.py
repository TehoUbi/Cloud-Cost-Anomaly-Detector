from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.session import get_db

router = APIRouter()


@router.get("/")
async def list_alerts(
    delivered: bool | None = Query(None, description="Filter by delivery status"),
    limit: int = Query(50, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Return alert log entries, optionally filtered by delivery status."""
    # TODO: Query AlertLog table
    raise NotImplementedError


@router.post("/{alert_id}/retry")
async def retry_alert(alert_id: str, db: AsyncSession = Depends(get_db)):
    """Manually retry delivery of a failed alert."""
    # TODO: Fetch AlertLog, call notifier.send_alert with the associated AnomalyResult
    raise NotImplementedError
