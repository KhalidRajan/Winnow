import httpx
import pytest
import respx

from concierge.auth import AuthError, TokenProvider
from concierge.config import Settings

AUTH_URL = "https://auth.test/token"


def _settings():
    return Settings(
        shopify_client_id="cid",
        shopify_client_secret="secret",
        agent_profile_url="https://example.com/profile.json",
        llm_api_key="key",
        auth_endpoint=AUTH_URL,
    )


class FakeClock:
    """A controllable monotonic clock for exercising the expiry logic."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@respx.mock
def test_first_call_fetches_token():
    route = respx.post(AUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok1", "expires_in": 3600}
        )
    )
    provider = TokenProvider(_settings(), clock=FakeClock())

    assert provider.get_token() == "tok1"
    assert route.call_count == 1


@respx.mock
def test_second_call_within_ttl_uses_cache():
    route = respx.post(AUTH_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "tok1", "expires_in": 3600}
        )
    )
    clock = FakeClock()
    provider = TokenProvider(_settings(), clock=clock)

    provider.get_token()
    clock.advance(100)  # well within the 3600s TTL

    assert provider.get_token() == "tok1"
    assert route.call_count == 1  # no second network call


@respx.mock
def test_expired_token_triggers_refresh():
    route = respx.post(AUTH_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok2", "expires_in": 3600}),
        ]
    )
    clock = FakeClock()
    provider = TokenProvider(_settings(), clock=clock)

    assert provider.get_token() == "tok1"
    clock.advance(3600)  # past expiry (even accounting for the safety margin)

    assert provider.get_token() == "tok2"
    assert route.call_count == 2


@respx.mock
def test_force_refresh_bypasses_cache():
    route = respx.post(AUTH_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok2", "expires_in": 3600}),
        ]
    )
    provider = TokenProvider(_settings(), clock=FakeClock())

    assert provider.get_token() == "tok1"
    assert provider.get_token(force_refresh=True) == "tok2"
    assert route.call_count == 2


@respx.mock
def test_non_200_raises_with_body():
    respx.post(AUTH_URL).mock(return_value=httpx.Response(401, text="invalid_client"))
    provider = TokenProvider(_settings(), clock=FakeClock())

    with pytest.raises(AuthError) as exc:
        provider.get_token()

    message = str(exc.value)
    assert "401" in message
    assert "invalid_client" in message  # body surfaced for debugging


@respx.mock
def test_missing_access_token_raises():
    respx.post(AUTH_URL).mock(
        return_value=httpx.Response(200, json={"expires_in": 3600})
    )
    provider = TokenProvider(_settings(), clock=FakeClock())

    with pytest.raises(AuthError):
        provider.get_token()
