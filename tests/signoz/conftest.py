import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.integrations.signoz.schemas import Scope, TimeRange
from src.integrations.signoz.settings import SigNozSettings

FIXTURES = Path(__file__).parent / "fixtures"
TEST_API_KEY = "sz-test-key-0123456789abcdef"
HOST_SCOPE = Scope(identity="host", values=("vm-1",))
SERVICE_SCOPE = Scope(identity="service", values=("inquest-demo",))


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def settings() -> SigNozSettings:
    return SigNozSettings(
        base_url="http://signoz.test", api_key=TEST_API_KEY, max_response_bytes=50_000
    )


@pytest.fixture
def time_range() -> TimeRange:
    return TimeRange(
        start=datetime(2026, 10, 14, 14, 0, tzinfo=UTC),
        end=datetime(2026, 10, 14, 15, 0, tzinfo=UTC),
    )
