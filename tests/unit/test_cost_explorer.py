"""The Cost Explorer client: pagination, parsing, and what it does and does not retry."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from src.config import CostProvider, Settings
from src.utils.cost_explorer import (
    AwsCostExplorerProvider,
    CostExplorerError,
    ServiceCost,
    SyntheticCostProvider,
    get_cost_provider,
)


def page(day: str, groups: dict[str, str], next_token: str | None = None) -> dict:
    body = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": day, "End": day},
                "Groups": [
                    {
                        "Keys": [service],
                        "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}},
                    }
                    for service, amount in groups.items()
                ],
            }
        ]
    }
    if next_token:
        body["NextPageToken"] = next_token
    return body


class StubClient:
    """Records calls and replays canned pages or raises canned errors."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        item = self.responses.pop(0) if self.responses else {"ResultsByTime": []}
        if isinstance(item, BaseException):
            raise item
        return item


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "GetCostAndUsage")


def provider(responses: list, **overrides) -> tuple[AwsCostExplorerProvider, StubClient]:
    settings = Settings(_env_file=None, cost_explorer_max_retries=3, **overrides)  # type: ignore[call-arg]
    stub = StubClient(responses)
    aws = AwsCostExplorerProvider(settings, client=stub)
    # Retries are real code paths; just do not sit through the ladder.
    object.__setattr__(aws._policy, "base_delay", 0.001)
    object.__setattr__(aws._policy, "max_delay", 0.001)
    return aws, stub


class TestParsing:
    def test_parses_a_single_page(self):
        aws, _ = provider([page("2026-10-01", {"AmazonEC2": "12.5", "AWSLambda": "3.25"})])
        rows = aws.fetch_daily_costs(days_back=1, end_date=date(2026, 10, 2))
        assert {(row.service, row.amount) for row in rows} == {
            ("AmazonEC2", Decimal("12.5")),
            ("AWSLambda", Decimal("3.25")),
        }

    def test_amount_is_decimal_not_float(self):
        """Parsing through float would lose the sub-cent precision AWS returns."""
        aws, _ = provider([page("2026-10-01", {"AmazonEC2": "0.123456"})])
        row = aws.fetch_daily_costs(days_back=1)[0]
        assert isinstance(row.amount, Decimal)
        assert row.amount == Decimal("0.123456")

    def test_usage_date_comes_from_the_period_start(self):
        aws, _ = provider([page("2026-09-15", {"AmazonEC2": "1.0"})])
        assert aws.fetch_daily_costs(days_back=1)[0].usage_date == date(2026, 9, 15)

    def test_exact_zeros_are_dropped(self):
        """Cost Explorer returns a zero for every service in the account every
        day; keeping them flattens the baseline."""
        aws, _ = provider([page("2026-10-01", {"AmazonEC2": "12.5", "AmazonSES": "0"})])
        rows = aws.fetch_daily_costs(days_back=1)
        assert [row.service for row in rows] == ["AmazonEC2"]

    def test_groups_without_keys_are_skipped(self):
        body = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-02"},
                    "Groups": [
                        {"Keys": [], "Metrics": {"UnblendedCost": {"Amount": "5"}}},
                        {
                            "Keys": ["AmazonEC2"],
                            "Metrics": {"UnblendedCost": {"Amount": "5", "Unit": "USD"}},
                        },
                    ],
                }
            ]
        }
        aws, _ = provider([body])
        assert len(aws.fetch_daily_costs(days_back=1)) == 1

    def test_missing_amount_is_skipped(self):
        body = {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-02"},
                    "Groups": [{"Keys": ["AmazonEC2"], "Metrics": {"UnblendedCost": {}}}],
                }
            ]
        }
        aws, _ = provider([body])
        assert aws.fetch_daily_costs(days_back=1) == []

    def test_empty_response_returns_nothing(self):
        aws, _ = provider([{"ResultsByTime": []}])
        assert aws.fetch_daily_costs(days_back=7) == []


class TestRequestShape:
    def test_end_is_exclusive_and_start_is_derived(self):
        aws, stub = provider([page("2026-10-01", {"AmazonEC2": "1"})])
        aws.fetch_daily_costs(days_back=30, end_date=date(2026, 10, 31))
        assert stub.calls[0]["TimePeriod"] == {"Start": "2026-10-01", "End": "2026-10-31"}

    def test_groups_daily_by_service(self):
        aws, stub = provider([page("2026-10-01", {"AmazonEC2": "1"})])
        aws.fetch_daily_costs(days_back=1)
        assert stub.calls[0]["Granularity"] == "DAILY"
        assert stub.calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]

    def test_filters_to_usage_records(self):
        """Credits and refunds post as negatives and read as a spend cliff."""
        aws, stub = provider([page("2026-10-01", {"AmazonEC2": "1"})])
        aws.fetch_daily_costs(days_back=1)
        assert stub.calls[0]["Filter"]["Dimensions"]["Values"] == ["Usage"]

    def test_metric_is_configurable(self):
        aws, stub = provider(
            [
                {
                    "ResultsByTime": [
                        {
                            "TimePeriod": {"Start": "2026-10-01", "End": "2026-10-02"},
                            "Groups": [
                                {
                                    "Keys": ["AmazonEC2"],
                                    "Metrics": {"AmortizedCost": {"Amount": "9.0", "Unit": "USD"}},
                                }
                            ],
                        }
                    ]
                }
            ],
            cost_explorer_metric="AmortizedCost",
        )
        rows = aws.fetch_daily_costs(days_back=1)
        assert stub.calls[0]["Metrics"] == ["AmortizedCost"]
        assert rows[0].amount == Decimal("9.0")


class TestPagination:
    def test_follows_next_page_token(self):
        """A single unpaginated call silently loses every service past page one."""
        aws, stub = provider(
            [
                page("2026-10-01", {"AmazonEC2": "1"}, next_token="t1"),
                page("2026-10-02", {"AWSLambda": "2"}, next_token="t2"),
                page("2026-10-03", {"AmazonS3": "3"}),
            ]
        )
        rows = aws.fetch_daily_costs(days_back=3)
        assert len(rows) == 3
        assert len(stub.calls) == 3

    def test_token_is_passed_back(self):
        aws, stub = provider(
            [page("2026-10-01", {"AmazonEC2": "1"}, next_token="abc"), page("2026-10-02", {})]
        )
        aws.fetch_daily_costs(days_back=2)
        assert "NextPageToken" not in stub.calls[0]
        assert stub.calls[1]["NextPageToken"] == "abc"

    def test_stops_at_the_page_ceiling(self):
        aws, stub = provider(
            [page("2026-10-01", {"AmazonEC2": "1"}, next_token="loop") for _ in range(80)]
        )
        aws.fetch_daily_costs(days_back=1)
        assert len(stub.calls) == 50


class TestRetryBehaviour:
    @pytest.mark.parametrize(
        "code", ["ThrottlingException", "RequestLimitExceeded", "ServiceUnavailable"]
    )
    def test_retries_throttling_and_server_faults(self, code):
        aws, stub = provider([client_error(code), page("2026-10-01", {"AmazonEC2": "1"})])
        assert len(aws.fetch_daily_costs(days_back=1)) == 1
        assert len(stub.calls) == 2

    def test_retries_connection_failures(self):
        aws, stub = provider(
            [
                EndpointConnectionError(endpoint_url="https://ce.us-east-1.amazonaws.com"),
                page("2026-10-01", {"AmazonEC2": "1"}),
            ]
        )
        assert len(aws.fetch_daily_costs(days_back=1)) == 1
        assert len(stub.calls) == 2

    @pytest.mark.parametrize(
        "code", ["AccessDeniedException", "ValidationException", "InvalidNextTokenException"]
    )
    def test_does_not_retry_permanent_errors(self, code):
        """Retrying AccessDenied just delays the failure by the whole ladder."""
        aws, stub = provider([client_error(code)])
        with pytest.raises(CostExplorerError, match="rejected"):
            aws.fetch_daily_costs(days_back=1)
        assert len(stub.calls) == 1

    def test_gives_up_after_the_retry_budget(self):
        aws, stub = provider([client_error("ThrottlingException")] * 5)
        with pytest.raises(CostExplorerError, match="unavailable after 3 attempts"):
            aws.fetch_daily_costs(days_back=1)
        assert len(stub.calls) == 3

    def test_failure_is_wrapped_in_a_domain_error(self):
        """So the API layer can map it to a 502 instead of a 500."""
        aws, _ = provider([client_error("AccessDeniedException")])
        with pytest.raises(CostExplorerError) as caught:
            aws.fetch_daily_costs(days_back=1)
        assert isinstance(caught.value.__cause__, ClientError)


class TestSyntheticProvider:
    def test_is_deterministic(self):
        first = SyntheticCostProvider().fetch_daily_costs(days_back=20, end_date=date(2026, 10, 1))
        second = SyntheticCostProvider().fetch_daily_costs(days_back=20, end_date=date(2026, 10, 1))
        assert [row.amount for row in first] == [row.amount for row in second]

    def test_seed_changes_the_series(self):
        first = SyntheticCostProvider(seed=1).fetch_daily_costs(days_back=20)
        second = SyntheticCostProvider(seed=2).fetch_daily_costs(days_back=20)
        assert [row.amount for row in first] != [row.amount for row in second]

    def test_covers_the_requested_window(self):
        rows = SyntheticCostProvider().fetch_daily_costs(days_back=10, end_date=date(2026, 10, 11))
        assert {row.usage_date for row in rows} == {date(2026, 10, day) for day in range(1, 11)}

    def test_every_amount_is_positive(self):
        rows = SyntheticCostProvider(spike_probability=0.5).fetch_daily_costs(days_back=60)
        assert all(row.amount > 0 for row in rows)

    def test_series_has_no_discontinuity(self):
        """The growth trend used to wrap once a year, putting an 18% cliff in
        the data that the detector correctly reported as a step change."""
        rows = [
            row
            for row in SyntheticCostProvider(spike_probability=0.0).fetch_daily_costs(
                days_back=800, end_date=date(2027, 6, 1)
            )
            if row.service == "Amazon Relational Database Service"
        ]
        rows.sort(key=lambda row: row.usage_date)
        amounts = [float(row.amount) for row in rows]
        jumps = [abs(amounts[i] - amounts[i - 1]) / amounts[i - 1] for i in range(1, len(amounts))]
        assert max(jumps) < 0.20

    def test_plants_spikes_when_asked(self):
        rows = SyntheticCostProvider(spike_probability=1.0, spike_multiplier=5.0)
        baseline = SyntheticCostProvider(spike_probability=0.0)
        spiked = sum(float(r.amount) for r in rows.fetch_daily_costs(days_back=30))
        normal = sum(float(r.amount) for r in baseline.fetch_daily_costs(days_back=30))
        assert spiked > normal * 3

    def test_returns_service_cost_instances(self):
        rows = SyntheticCostProvider().fetch_daily_costs(days_back=2)
        assert all(isinstance(row, ServiceCost) for row in rows)
        assert rows[0].unit == "USD"


class TestProviderSelection:
    def test_synthetic_is_selected_by_config(self):
        settings = Settings(_env_file=None, cost_provider=CostProvider.SYNTHETIC)  # type: ignore[call-arg]
        assert isinstance(get_cost_provider(settings), SyntheticCostProvider)

    def test_aws_is_the_default(self):
        settings = Settings(_env_file=None, cost_provider=CostProvider.AWS)  # type: ignore[call-arg]
        assert isinstance(get_cost_provider(settings), AwsCostExplorerProvider)

    def test_aws_client_is_not_built_at_construction(self):
        """Constructing a boto3 client resolves credentials, which must not
        happen just because something imported the module."""
        settings = Settings(_env_file=None, cost_provider=CostProvider.AWS)  # type: ignore[call-arg]
        aws = get_cost_provider(settings)
        assert aws._client is None  # type: ignore[union-attr]
