from sqlalchemy import Column, String, Float, Date, DateTime, Boolean, func
from src.db.session import Base


class CostRecord(Base):
    __tablename__ = "cost_records"

    id = Column(String, primary_key=True)
    service = Column(String, nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    amount = Column(Float, nullable=False)
    unit = Column(String, default="USD")
    zscore = Column(Float, nullable=True)
    is_anomaly = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id = Column(String, primary_key=True)
    cost_record_id = Column(String, nullable=False, index=True)
    delivered = Column(Boolean, default=False)
    attempts = Column(Float, default=0)
    last_attempted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
