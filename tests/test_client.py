"""Client-layer tests: tag handling, token loading, retry and graceful 403."""

from __future__ import annotations

import json

import httpx
import pytest

from coc_telemetry.client import (
    TOKEN_ENV_VAR,
    CocClient,
    InvalidTagError,
    MissingTokenError,
    load_token,
    normalise_tag,
    url_tag,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#202VL9GR", "#202VL9GR"),
        ("202VL9GR", "#202VL9GR"),
        ("  #202vl9gr  ", "#202VL9GR"),
        ("#2u9qlcy8y", "#2U9QLCY8Y"),
        ("O2O2VL9GR", "#0202VL9GR"),  # O folds to zero before validation
    ],
)
def test_normalise_tag_accepts_and_canonicalises(raw: str, expected: str) -> None:
    assert normalise_tag(raw) == expected


@pytest.mark.parametrize("raw", ["", "#", "#ABC", "#202VL9GR!", "#IIII", "#SSSS"])
def test_normalise_tag_rejects_bad_input(raw: str) -> None:
    with pytest.raises(InvalidTagError):
        normalise_tag(raw)


def test_url_tag_percent_encodes_hash() -> None:
    assert url_tag("#202VL9GR") == "%23202VL9GR"


def test_load_token_fails_loudly_when_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env to fall back on
    with pytest.raises(MissingTokenError, match=TOKEN_ENV_VAR):
        load_token()


def test_load_token_rejects_a_password_shaped_value(monkeypatch, tmp_path) -> None:
    """A short non-JWT is almost certainly a password pasted by mistake."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(TOKEN_ENV_VAR, "aaygmtx4")
    with pytest.raises(MissingTokenError, match="JSON Web Token"):
        load_token()


def test_error_message_never_leaks_the_token(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(TOKEN_ENV_VAR, "not-a-jwt-but-secret")
    with pytest.raises(MissingTokenError) as excinfo:
        load_token()
    assert "not-a-jwt-but-secret" not in str(excinfo.value)


def test_dotenv_does_not_override_real_environment(monkeypatch, tmp_path) -> None:
    """CI injects the secret; a stale local .env must never win."""
    (tmp_path / ".env").write_text(f"{TOKEN_ENV_VAR}=eyJstale\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(TOKEN_ENV_VAR, "eyJfrom-ci")
    assert load_token() == "eyJfrom-ci"


def test_sends_bearer_auth_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"state": "notInWar"})

    with CocClient("eyJtest", transport=httpx.MockTransport(handler)) as client:
        client.current_war("#2U9QLCY8Y")
    assert seen["authorization"] == "Bearer eyJtest"


def test_private_war_log_returns_403_result_rather_than_raising() -> None:
    """A private war log must degrade, and its body must still reach capture."""
    body = {"reason": "accessDenied", "message": "war log is private"}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=body)

    with CocClient("eyJtest", transport=httpx.MockTransport(handler)) as client:
        result = client.war_log("#2U9QLCY8Y")

    assert result.access_denied
    assert not result.ok
    assert result.data == body
    assert json.loads(result.raw) == body


def test_retries_429_then_succeeds(monkeypatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("coc_telemetry.client.time.sleep", slept.append)
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, headers={"retry-after": "2"}, json={})
        return httpx.Response(200, json={"state": "inWar"})

    with CocClient("eyJtest", transport=httpx.MockTransport(handler)) as client:
        result = client.current_war("#2U9QLCY8Y")

    assert result.ok
    assert attempts == 3
    assert slept == [2.0, 2.0]  # Retry-After honoured rather than blind backoff


def test_gives_up_after_max_retries(monkeypatch) -> None:
    monkeypatch.setattr("coc_telemetry.client.time.sleep", lambda _s: None)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    from coc_telemetry.client import ApiRequestError

    with (
        CocClient("eyJtest", max_retries=2, transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ApiRequestError, match="after 3 attempts"),
    ):
        client.current_war("#2U9QLCY8Y")


def test_cwl_round_endpoint_slug_carries_the_war_tag() -> None:
    """One CWL poll writes several round wars, so the slug must disambiguate them."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": "inWar"})

    with CocClient("eyJtest", transport=httpx.MockTransport(handler)) as client:
        result = client.league_war("#2PP0JCCL")

    assert result.endpoint == "cwlwar_2PP0JCCL"
