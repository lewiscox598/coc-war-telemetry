"""Thin hand-written client for the Clash of Clans API via the RoyaleAPI proxy.

Deliberately not coc.py. The response schema stays under our control because the
derived database must be rebuildable from raw captures, and the source cannot be
re-queried once a war ends.

Never log the token or any header containing it. Request URLs carry only tags.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import httpx

# Paths are identical to https://api.clashofclans.com/v1; only the host differs.
BASE_URL: Final = "https://proxy.royaleapi.dev/v1"

# The developer key must whitelist this address rather than the caller's own IP:
# the proxy makes the upstream request to Supercell, so this is the address
# Supercell sees. Documented here because a mismatch fails silently at runtime.
PROXY_WHITELIST_IP: Final = "45.79.218.79"

# Supercell tags use a restricted alphabet. Notably it excludes O, I, S and 1,
# which is why user-supplied tags need O -> 0 folding before validation.
TAG_ALPHABET: Final = frozenset("0289CGJLPQRUVY")

TOKEN_ENV_VAR: Final = "COC_API_TOKEN"

DEFAULT_TIMEOUT: Final = 30.0
MAX_RETRIES: Final = 4
BACKOFF_BASE_SECONDS: Final = 1.0

# Statuses we expect and hand back to the caller rather than raising on, so the
# raw body still reaches the capture layer. 403 in particular is the documented
# response for a private war log and must degrade gracefully.
EXPECTED_STATUSES: Final = frozenset({200, 403, 404})


class CocApiError(RuntimeError):
    """Base class for all client errors."""


class MissingTokenError(CocApiError):
    """The API token is not configured."""


class InvalidTagError(CocApiError, ValueError):
    """A tag is malformed or contains characters outside the Supercell alphabet."""


class ApiRequestError(CocApiError):
    """A request failed after exhausting retries."""


def normalise_tag(raw: str) -> str:
    """Return a canonical ``#TAG``: uppercase, O folded to 0, validated.

    Accepts tags with or without the leading ``#`` and with surrounding whitespace.
    Raises InvalidTagError rather than silently passing a bad tag to the API, since
    a typo would otherwise surface as a confusing 404 much later.
    """
    if not isinstance(raw, str):
        raise InvalidTagError(f"tag must be a string, got {type(raw).__name__}")

    tag = raw.strip().upper().lstrip("#").replace("O", "0")

    if not tag:
        raise InvalidTagError("tag is empty")
    if not (3 <= len(tag) <= 15):
        raise InvalidTagError(f"tag #{tag} has implausible length {len(tag)}")

    bad = sorted(set(tag) - TAG_ALPHABET)
    if bad:
        raise InvalidTagError(
            f"tag #{tag} contains characters outside the Supercell alphabet: {''.join(bad)}"
        )
    return f"#{tag}"


def url_tag(raw: str) -> str:
    """Normalise a tag and percent-encode it for use in a URL path segment."""
    return quote(normalise_tag(raw), safe="")


def _slug_tag(raw: str) -> str:
    """Filesystem-safe form of a tag, for embedding in capture filenames."""
    return normalise_tag(raw).lstrip("#")


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a local .env into os.environ if not already set.

    Hand-rolled to avoid a python-dotenv dependency. Existing environment variables
    win, so CI (which injects the token as a secret) is never overridden by a
    stale local file.
    """
    env_path = path or Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_token() -> str:
    """Return the API token, failing loudly if it is absent or obviously wrong.

    The error message never includes the token value.
    """
    load_dotenv()
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise MissingTokenError(
            f"{TOKEN_ENV_VAR} is not set. Create a key at "
            "https://developer.clashofclans.com/#/account whitelisted to "
            f"{PROXY_WHITELIST_IP}, then put it in .env or the repo secret."
        )
    if not token.startswith("eyJ"):
        raise MissingTokenError(
            f"{TOKEN_ENV_VAR} does not look like a JSON Web Token (expected it to "
            "start with 'eyJ'). Check you copied the whole key, not a password."
        )
    return token


@dataclass(frozen=True, slots=True)
class ApiResult:
    """One HTTP response, carrying both the parsed body and the unmodified bytes.

    ``raw`` is what the capture layer gzips verbatim; ``data`` is for ingest. Keeping
    both on one object means a capture is always byte-faithful to what we parsed.
    """

    endpoint: str
    status_code: int
    raw: bytes
    data: dict[str, Any]
    fetched_at: datetime
    rate_limit: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code == 200

    @property
    def access_denied(self) -> bool:
        """True for a private war log, which is expected and must not be fatal."""
        return self.status_code == 403


class CocClient:
    """Synchronous client. One instance per poll run."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token if token is not None else load_token()
        self._max_retries = max_retries
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "coc-war-telemetry/0.1 (+https://github.com/)",
            },
        )

    def __enter__(self) -> CocClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _sleep_for(self, response: httpx.Response | None, attempt: int) -> float:
        """Seconds to wait before the next attempt.

        Honours Retry-After when the server sends one; otherwise exponential backoff
        with jitter. Our volume sits far below the per-second limit, so a 429 means
        something unexpected and is worth backing off properly rather than hammering.
        """
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    return max(0.0, float(retry_after))
                except ValueError:
                    pass
        return BACKOFF_BASE_SECONDS * (2**attempt) + random.uniform(0, 0.5)

    def _get(self, path: str, endpoint: str) -> ApiResult:
        """GET a path, retrying on 429/5xx/transport errors.

        Returns an ApiResult for any expected status so that even a 403 body reaches
        the capture layer. Raises ApiRequestError only when retries are exhausted.
        """
        last_error: str = "no attempt made"

        for attempt in range(self._max_retries + 1):
            response: httpx.Response | None = None
            try:
                response = self._client.get(path)
            except httpx.TransportError as exc:
                # Message deliberately excludes headers; URLs carry only tags.
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code in EXPECTED_STATUSES:
                    return ApiResult(
                        endpoint=endpoint,
                        status_code=response.status_code,
                        raw=response.content,
                        data=self._parse(response),
                        fetched_at=datetime.now(UTC),
                        rate_limit=response.headers.get("x-ratelimit-limit"),
                    )
                if response.status_code not in (429, *range(500, 600)):
                    raise ApiRequestError(
                        f"GET {endpoint} returned unexpected status "
                        f"{response.status_code}: {response.text[:200]}"
                    )
                last_error = f"HTTP {response.status_code}"

            if attempt < self._max_retries:
                time.sleep(self._sleep_for(response, attempt))

        raise ApiRequestError(
            f"GET {endpoint} failed after {self._max_retries + 1} attempts ({last_error})"
        )

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except ValueError as exc:
            raise ApiRequestError(f"response was not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ApiRequestError(f"expected a JSON object, got {type(parsed).__name__}")
        return parsed

    # --- Endpoints -------------------------------------------------------

    def current_war(self, clan_tag: str) -> ApiResult:
        """Regular war state. Reports notInWar during CWL; caller falls back."""
        return self._get(f"/clans/{url_tag(clan_tag)}/currentwar", "currentwar")

    def league_group(self, clan_tag: str) -> ApiResult:
        """CWL group and its round war tags. 404 when not in CWL."""
        return self._get(f"/clans/{url_tag(clan_tag)}/currentwar/leaguegroup", "leaguegroup")

    def league_war(self, war_tag: str) -> ApiResult:
        """One CWL round. These wars carry a warTag and no attacksPerMember."""
        return self._get(f"/clanwarleagues/wars/{url_tag(war_tag)}", f"cwlwar_{_slug_tag(war_tag)}")

    def war_log(self, clan_tag: str) -> ApiResult:
        """War log. Returns a 403 result (not an exception) when the log is private."""
        return self._get(f"/clans/{url_tag(clan_tag)}/warlog", "warlog")

    def clan_members(self, clan_tag: str) -> ApiResult:
        return self._get(f"/clans/{url_tag(clan_tag)}/members", "members")

    def clan(self, clan_tag: str) -> ApiResult:
        return self._get(f"/clans/{url_tag(clan_tag)}", "clan")

    def player(self, player_tag: str) -> ApiResult:
        return self._get(f"/players/{url_tag(player_tag)}", "player")

    def capital_raid_seasons(self, clan_tag: str, *, limit: int = 10) -> ApiResult:
        return self._get(
            f"/clans/{url_tag(clan_tag)}/capitalraidseasons?limit={limit}",
            "capitalraidseasons",
        )
