"""Bearer-token acquisition for the Global Catalog, with caching + refresh.

A single short CLI run only needs one token, but the same provider serves the
Full-scope web server, which outlives the 60-minute token and makes many calls —
so we cache the token and refresh it when it nears expiry.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import httpx

from concierge.config import Settings

# Refresh this many seconds before the token actually expires, so an in-flight
# request never races the expiry boundary.
_EXPIRY_SAFETY_MARGIN = 60.0
_DEFAULT_TTL = 3600.0


class AuthError(Exception):
    """Raised when a bearer token cannot be obtained."""


class TokenProvider:
    """Fetches and caches a client-credentials bearer token, refreshing on expiry.

    ``clock`` defaults to ``time.monotonic`` (immune to wall-clock jumps) and is
    injectable so tests can advance time deterministically.
    """

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = http_client or httpx.Client(timeout=30.0)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid bearer token, fetching or refreshing if needed."""
        with self._lock:
            if (
                not force_refresh
                and self._token is not None
                and self._clock() < self._expires_at
            ):
                return self._token

            token, ttl = self._fetch()
            self._token = token
            self._expires_at = self._clock() + max(0.0, ttl - _EXPIRY_SAFETY_MARGIN)
            return token

    def _fetch(self) -> tuple[str, float]:
        # RFC 6749 client-credentials grant (form-encoded). If Shopify's endpoint
        # expects a JSON body instead, switch `data=` to `json=` here.
        try:
            response = self._client.post(
                self._settings.auth_endpoint,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._settings.shopify_client_id,
                    "client_secret": self._settings.shopify_client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthError(f"Token request failed: {exc}") from exc

        if response.status_code != 200:
            raise AuthError(
                f"Token endpoint returned {response.status_code}: {response.text}"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AuthError(f"Token response missing 'access_token': {payload}")

        try:
            ttl = float(payload.get("expires_in", _DEFAULT_TTL))
        except (TypeError, ValueError):
            ttl = _DEFAULT_TTL
        return token, ttl

    def close(self) -> None:
        self._client.close()
