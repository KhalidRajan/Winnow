import json
from pathlib import Path

import httpx
import pytest
import respx

from concierge.config import Settings
from concierge.mcp_client import McpClient, McpError

MCP_URL = "https://catalog.test/mcp"
_FIXTURE = Path(__file__).parent / "fixtures" / "search_response.json"


def _settings():
    return Settings(
        shopify_client_id="cid",
        shopify_client_secret="secret",
        agent_profile_url="https://example.com/profile.json",
        llm_api_key="key",
        mcp_endpoint=MCP_URL,
    )


class StubTokenProvider:
    def __init__(self, token="tok"):
        self.token = token

    def get_token(self, *, force_refresh=False):
        return self.token


def _client():
    return McpClient(
        _settings(),
        StubTokenProvider(),
        sleep=lambda _: None,  # don't actually back off in tests
    )


def _fixture_body():
    return json.loads(_FIXTURE.read_text())


@respx.mock
def test_call_sends_wellformed_jsonrpc_with_auth_and_profile():
    route = respx.post(MCP_URL).mock(
        return_value=httpx.Response(200, json=_fixture_body())
    )
    _client().search_catalog({"query": "jacket"})

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer tok"
    sent = json.loads(request.content)
    assert sent["jsonrpc"] == "2.0"
    assert sent["method"] == "tools/call"
    assert sent["params"]["name"] == "search_catalog"
    arguments = sent["params"]["arguments"]
    # caller-supplied args are preserved, profile merged in alongside
    assert arguments["query"] == "jacket"
    assert (
        arguments["meta"]["ucp-agent"]["profile"] == "https://example.com/profile.json"
    )


@respx.mock
def test_jsonrpc_error_raises_mcp_error():
    respx.post(MCP_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "error": {"code": -32000, "message": "bad"},
            },
        )
    )
    with pytest.raises(McpError) as exc:
        _client().search_catalog({"query": "x"})
    assert "bad" in str(exc.value)


@respx.mock
def test_retries_on_429_then_succeeds():
    route = respx.post(MCP_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=_fixture_body()),
        ]
    )
    result = _client().search_catalog({"query": "x"})
    assert route.call_count == 2
    assert "structuredContent" in result


@respx.mock
def test_gives_up_after_max_attempts():
    respx.post(MCP_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(McpError) as exc:
        _client().search_catalog({"query": "x"})
    assert "503" in str(exc.value)


@respx.mock
def test_missing_result_raises():
    respx.post(MCP_URL).mock(
        return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})
    )
    with pytest.raises(McpError):
        _client().search_catalog({"query": "x"})
