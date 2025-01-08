"""
AWS Cost Explorer client wrapper.

Fetches per-service daily costs for a given date range.
"""

import boto3
from datetime import date, timedelta
from dataclasses import dataclass


@dataclass
class ServiceCost:
    service: str
    date: str       # YYYY-MM-DD
    amount: float
    unit: str       # e.g. "USD"


def get_cost_explorer_client():
    return boto3.client("ce")


def fetch_daily_costs(
    days_back: int = 30,
    end_date: date | None = None,
) -> list[ServiceCost]:
    """
    Fetch daily cost breakdown per AWS service for the past `days_back` days.

    Returns a flat list of ServiceCost entries sorted by date ascending.
    """
    ce = get_cost_explorer_client()
    end = end_date or date.today()
    start = end - timedelta(days=days_back)

    response = ce.get_cost_and_usage(
        TimePeriod={
            "Start": start.isoformat(),
            "End": end.isoformat(),
        },
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    results: list[ServiceCost] = []
    for period in response.get("ResultsByTime", []):
        period_start = period["TimePeriod"]["Start"]
        for group in period.get("Groups", []):
            service_name = group["Keys"][0]
            amount_str = group["Metrics"]["UnblendedCost"]["Amount"]
            unit = group["Metrics"]["UnblendedCost"]["Unit"]
            results.append(ServiceCost(
                service=service_name,
                date=period_start,
                amount=float(amount_str),
                unit=unit,
            ))

    return results
