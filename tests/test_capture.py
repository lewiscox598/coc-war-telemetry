"""Capture-layer tests: change detection, determinism, and rebuild ordering."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

from coc_telemetry.capture import CaptureStore, parse_capture_path
from coc_telemetry.client import ApiResult


def make_result(endpoint: str, payload: dict, when: datetime, status: int = 200) -> ApiResult:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return ApiResult(
        endpoint=endpoint,
        status_code=status,
        raw=raw,
        data=payload,
        fetched_at=when,
    )


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 9, 20, hour, minute, second, tzinfo=UTC)


def test_writes_capture_to_date_partitioned_path(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path)
    path = store.write(make_result("currentwar", {"state": "inWar"}, at(14, 30)))

    assert path is not None
    assert path.relative_to(tmp_path).as_posix() == (
        "2026/09/20/currentwar-20260920T143000Z.json.gz"
    )
    assert json.loads(gzip.decompress(path.read_bytes())) == {"state": "inWar"}


def test_identical_response_is_not_captured_twice(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path)
    payload = {"state": "inWar", "clan": {"stars": 12}}

    first = store.write(make_result("currentwar", payload, at(14, 30)))
    second = store.write(make_result("currentwar", payload, at(14, 45)))

    assert first is not None
    assert second is None, "unchanged response should not produce a new capture"
    assert len(store.paths("currentwar")) == 1


def test_changed_response_is_captured(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path)
    store.write(make_result("currentwar", {"state": "preparation"}, at(14, 30)))
    store.write(make_result("currentwar", {"state": "inWar"}, at(14, 45)))

    assert len(store.paths("currentwar")) == 2


def test_change_detection_is_per_endpoint(tmp_path: Path) -> None:
    """A members capture must not suppress an identical-bodied player capture."""
    store = CaptureStore(tmp_path)
    payload = {"items": []}

    assert store.write(make_result("members", payload, at(3, 0))) is not None
    assert store.write(make_result("player", payload, at(3, 0))) is not None


def test_gzip_is_deterministic_across_capture_times(tmp_path: Path) -> None:
    """mtime=0 keeps identical content byte-identical, so git diffs stay honest."""
    store_a, store_b = CaptureStore(tmp_path / "a"), CaptureStore(tmp_path / "b")
    payload = {"state": "warEnded"}

    path_a = store_a.write(make_result("currentwar", payload, at(14, 30)))
    path_b = store_b.write(make_result("currentwar", payload, at(23, 59)))

    assert path_a is not None and path_b is not None
    assert path_a.read_bytes() == path_b.read_bytes()


def test_capture_time_round_trips_through_the_filename(tmp_path: Path) -> None:
    """Rebuild derives first_seen from the filename, so this must be exact."""
    store = CaptureStore(tmp_path)
    moment = at(14, 30, 45)
    path = store.write(make_result("currentwar", {"state": "inWar"}, moment))

    assert path is not None
    endpoint, recovered = parse_capture_path(path)
    assert endpoint == "currentwar"
    assert recovered == moment
    assert store.read(path).captured_at == moment


def test_iterates_chronologically_across_endpoints_and_days(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path)
    store.write(make_result("currentwar", {"n": 2}, at(14, 30)))
    store.write(make_result("members", {"n": 1}, at(3, 0)))
    store.write(make_result("player", {"n": 3}, datetime(2026, 9, 21, 3, 0, tzinfo=UTC)))

    captures = list(store.iter_captures())
    assert [c.data["n"] for c in captures] == [1, 2, 3]


def test_cwl_rounds_captured_in_the_same_second_are_ordered_stably(
    tmp_path: Path,
) -> None:
    """One CWL poll writes every round war at once; ordering must be deterministic."""
    store = CaptureStore(tmp_path)
    for war_tag in ["cwlwar_2PP0JCCL", "cwlwar_20VJRLC9", "cwlwar_8RQGVJ2P"]:
        store.write(make_result(war_tag, {"tag": war_tag}, at(14, 30)))

    first = [c.endpoint for c in store.iter_captures()]
    second = [c.endpoint for c in store.iter_captures()]
    assert first == second == sorted(first)


def test_error_bodies_are_captured_and_recognisable(tmp_path: Path) -> None:
    """A 403 war log is evidence too, and must be distinguishable on rebuild."""
    store = CaptureStore(tmp_path)
    body = {"reason": "accessDenied", "message": "war log is private"}
    path = store.write(make_result("warlog", body, at(3, 0), status=403))

    assert path is not None
    capture = store.read(path)
    assert capture.is_error
    assert capture.reason == "accessDenied"


def test_successful_payload_is_not_mistaken_for_an_error(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path)
    path = store.write(make_result("currentwar", {"state": "notInWar"}, at(3, 0)))

    assert path is not None
    assert not store.read(path).is_error


def test_empty_store_reads_cleanly(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path / "does-not-exist")
    assert store.paths() == []
    assert store.latest_path("currentwar") is None
    assert list(store.iter_captures()) == []


def test_no_temp_files_are_left_behind(tmp_path: Path) -> None:
    store = CaptureStore(tmp_path)
    store.write(make_result("currentwar", {"state": "inWar"}, at(14, 30)))
    assert list(tmp_path.rglob("*.tmp")) == []
