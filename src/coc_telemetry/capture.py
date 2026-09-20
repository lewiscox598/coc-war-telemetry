"""Raw capture layer: the insurance policy.

Every poll writes the unmodified response body, gzipped, under
``data/raw/YYYY/MM/DD/<endpoint>-<timestamp>.json.gz`` -- but only when it differs
from the previous capture for that endpoint.

The source cannot be re-queried once a war ends, so if the derived schema turns out
wrong the database must be rebuildable from these files alone. Two properties make
that work:

* The capture timestamp lives in the *filename*, so a rebuild reconstructs the same
  ``first_seen`` values it would have recorded live. Never take capture time from the
  wall clock during a rebuild.
* Gzip is written with ``mtime=0`` so identical content yields identical bytes,
  keeping change-detection and diffs honest.
"""

from __future__ import annotations

import gzip
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from coc_telemetry.client import ApiResult

# Compact ISO-8601 basic format: lexically sortable and filesystem-safe.
TIMESTAMP_FORMAT: Final = "%Y%m%dT%H%M%SZ"

RAW_SUFFIX: Final = ".json.gz"


def format_timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, TIMESTAMP_FORMAT).replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Capture:
    """One archived response, reconstituted from disk."""

    endpoint: str
    captured_at: datetime
    data: dict[str, Any]
    path: Path

    @property
    def is_error(self) -> bool:
        """True when the body is an API error rather than a payload.

        The Clash of Clans API returns ``{"reason": ...}`` for errors and never
        includes a top-level ``reason`` on a success, so this is a safe discriminator.
        It is how a captured 403 (private war log) or 404 (not in CWL) is recognised
        on rebuild, since HTTP status is not part of the archived body.
        """
        return "reason" in self.data

    @property
    def reason(self) -> str | None:
        value = self.data.get("reason")
        return value if isinstance(value, str) else None


def capture_filename(endpoint: str, captured_at: datetime) -> str:
    return f"{endpoint}-{format_timestamp(captured_at)}{RAW_SUFFIX}"


def parse_capture_path(path: Path) -> tuple[str, datetime]:
    """Recover (endpoint, captured_at) from a capture path.

    Endpoint slugs never contain '-' (CWL rounds use ``cwlwar_<TAG>``), so splitting
    on the final hyphen is unambiguous.
    """
    name = path.name
    if not name.endswith(RAW_SUFFIX):
        raise ValueError(f"not a capture file: {path}")
    stem = name[: -len(RAW_SUFFIX)]
    endpoint, _, stamp = stem.rpartition("-")
    if not endpoint or not stamp:
        raise ValueError(f"malformed capture filename: {name}")
    return endpoint, parse_timestamp(stamp)


class CaptureStore:
    """Append-only archive of raw API responses rooted at ``data/raw``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- Writing ---------------------------------------------------------

    def write(self, result: ApiResult) -> Path | None:
        """Archive a response, or return None if it matches the previous capture.

        Deduplication compares decompressed bytes, so gzip framing never causes a
        spurious write.
        """
        previous = self.latest_path(result.endpoint)
        if previous is not None and _read_bytes(previous) == result.raw:
            return None

        directory = self.root / result.fetched_at.astimezone(UTC).strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / capture_filename(result.endpoint, result.fetched_at)

        # Write-then-rename so a killed runner cannot leave a truncated archive.
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(gzip.compress(result.raw, mtime=0))
        os.replace(temporary, target)
        return target

    # --- Reading ---------------------------------------------------------

    def paths(self, endpoint: str | None = None) -> list[Path]:
        """All capture paths in chronological order.

        Sorted by (captured_at, endpoint, name) so that a rebuild folds captures in a
        deterministic order even when several endpoints share a timestamp, as happens
        when one CWL poll writes every round war at once.
        """
        if not self.root.is_dir():
            return []
        found: list[tuple[datetime, str, str, Path]] = []
        for path in self.root.glob(f"*/*/*/*{RAW_SUFFIX}"):
            try:
                slug, captured_at = parse_capture_path(path)
            except ValueError:
                continue
            if endpoint is not None and slug != endpoint:
                continue
            found.append((captured_at, slug, path.name, path))
        found.sort(key=lambda item: item[:3])
        return [item[3] for item in found]

    def latest_path(self, endpoint: str) -> Path | None:
        paths = self.paths(endpoint)
        return paths[-1] if paths else None

    def read(self, path: Path) -> Capture:
        endpoint, captured_at = parse_capture_path(path)
        data = json.loads(_read_bytes(path).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"capture {path} is not a JSON object")
        return Capture(endpoint=endpoint, captured_at=captured_at, data=data, path=path)

    def iter_captures(self, endpoint: str | None = None) -> Iterator[Capture]:
        """Yield every capture in chronological order. This is what rebuild folds."""
        for path in self.paths(endpoint):
            yield self.read(path)


def _read_bytes(path: Path) -> bytes:
    with gzip.open(path, "rb") as handle:
        return handle.read()
