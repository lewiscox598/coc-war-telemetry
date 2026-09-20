from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coc_telemetry.capture import CaptureStore
from coc_telemetry.client import ApiResult

FIXTURES = Path(__file__).parent / "fixtures"

OUR_CLAN = "#2U9QLCY8Y"


@pytest.fixture
def fixture_json():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def store(tmp_path: Path) -> CaptureStore:
    return CaptureStore(tmp_path / "raw")


def make_result(endpoint: str, payload: dict, when: datetime, status: int = 200) -> ApiResult:
    return ApiResult(
        endpoint=endpoint,
        status_code=status,
        raw=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        data=payload,
        fetched_at=when,
    )


def at(hour: int, minute: int = 0, day: int = 21) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)
