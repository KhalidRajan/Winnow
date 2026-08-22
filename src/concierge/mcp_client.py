"""JSON-RPC 2.0 client for the Shopify Global Catalog MCP endpoint.

Framework-independent: given a ``TokenProvider`` it speaks the MCP ``tools/call``
protocol, injecting the bearer token and the UCP agent-profile URL on every
request, and retrying transient failures. No result caching (a catalog
constraint).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

import httpx

from concierge.auth import TokenProvider
from concierge.config import Settings

# Throttling plus the 5xx family that means "try again" rather than "you asked
# wrong". HTTPStatus is an IntEnum, so a plain ``response.status_code`` int still
# matches on membership.
_RETRYABLE_STATUS = {
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 0.5


class McpError(Exception):
    """Raised when the MCP endpoint returns a JSON-RPC error or fails to respond."""


class McpClient:
    def __init__(
        self,
        settings: Settings,
        token_provider: TokenProvider,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._tokens = token_provider
        self._client = http_client or httpx.Client(timeout=30.0)
        self._sleep = sleep
        self._request_id = 0

    def search_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._call("search_catalog", arguments)

    def lookup_catalog(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # Stubbed for Full scope; wired the same way as search_catalog.
        return self._call("lookup_catalog", arguments)

    def get_product(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._call("get_product", arguments)

    def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        # The UCP agent profile URI is required on every request, nested inside
        # params.arguments as meta -> ucp-agent -> profile (confirmed against the
        # live endpoint). The caller supplies the tool-specific argument keys
        # (e.g. "catalog"); we merge the profile alongside them.
        full_arguments: dict[str, Any] = {
            **arguments,
            "meta": {"ucp-agent": {"profile": self._settings.agent_profile_url}},
        }
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": full_arguments},
        }
        response = self._post_with_retry(payload)

        body = response.json()
        if body.get("error"):
            raise McpError(f"MCP returned an error: {body['error']}")
        result = body.get("result")
        if not isinstance(result, dict):
            raise McpError(f"MCP response missing 'result': {body}")
        return result

    def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        last_status: int | None = None
        for attempt in range(_MAX_ATTEMPTS):
            headers = {
                "Authorization": f"Bearer {self._tokens.get_token()}",
                "Content-Type": "application/json",
            }
            try:
                response = self._client.post(
                    self._settings.mcp_endpoint, json=payload, headers=headers
                )
            except httpx.HTTPError as exc:
                raise McpError(f"MCP request failed: {exc}") from exc

            if response.status_code not in _RETRYABLE_STATUS:
                return response

            last_status = response.status_code
            if attempt < _MAX_ATTEMPTS - 1:
                self._sleep(_BACKOFF_BASE * (2**attempt))

        raise McpError(
            f"MCP endpoint unavailable after {_MAX_ATTEMPTS} attempts "
            f"(last status {last_status})"
        )

    def close(self) -> None:
        self._client.close()
