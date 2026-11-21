"""The internal connectivity MCP granted to newly created WebUI aliases."""
from fastmcp import FastMCP

from .models import Problem

ALIAS = "switcher-dummy"
PORT = 8001


def create_dummy_mcp():
    server = FastMCP(ALIAS)

    @server.tool(annotations={"readOnlyHint": True})
    def hello() -> str:
        """Check the connection through LiteLLM."""
        return "Hello World"

    return server


async def ensure_registered(upstream, url):
    spec = {"server_name": ALIAS, "alias": ALIAS, "url": url, "transport": "http",
            "allow_all_keys": False, "available_on_public_internet": False}

    async def lookup():
        registry = await upstream.request("GET", "/v1/mcp/server")
        if not isinstance(registry, list):
            raise Problem(502, "LiteLLM MCP registry is unavailable")
        matches = [row for row in registry if ALIAS in (row.get("alias"), row.get("server_name"))]
        if not matches:
            return None
        if len(matches) != 1 or any(matches[0].get(k) != v for k, v in spec.items()) or not matches[0].get("server_id"):
            raise Problem(409, "The switcher-dummy registry entry does not match the configured private endpoint")
        return matches[0]["server_id"]

    server_id = await lookup()
    if server_id:
        return server_id
    try:
        await upstream.request("POST", "/v1/mcp/server", json=spec)
    except Problem:
        # A committed registration can outlive its HTTP response.
        server_id = await lookup()
        if server_id:
            return server_id
        raise
    server_id = await lookup()
    if not server_id:
        raise Problem(502, "LiteLLM did not confirm switcher-dummy registration")
    return server_id
