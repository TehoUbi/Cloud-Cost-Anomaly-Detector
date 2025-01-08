# integration test placeholder
# Requires a live DB and (optionally) AWS sandbox credentials.
# Run with: pytest tests/integration -m integration

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_poll_and_detect_end_to_end():
    """
    Full cycle: seed DB, run poll_and_detect, verify CostRecord persisted.
    TODO: Mock Cost Explorer response, assert DB row written, assert AlertLog on anomaly.
    """
    pytest.skip("Not yet implemented — requires DB fixture setup")
