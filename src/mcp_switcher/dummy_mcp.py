"""The internal connectivity MCP granted to newly created WebUI aliases."""
import uuid

from fastmcp import FastMCP

from .models import Problem

ALIAS = "switcher_dummy"
PORT = 8001


def create_dummy_mcp():
    server = FastMCP(ALIAS)

    @server.tool(annotations={"readOnlyHint": True})
    def hello_world() -> str:
        return "hello world"

    return server


async def ensure_registered(upstream, url):
    server_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    spec = {"server_id": server_id, "server_name": ALIAS, "alias": ALIAS, "url": url, "transport": "http",
            "allow_all_keys": False, "available_on_public_internet": False}

    async def lookup():
        registry = await upstream.request("GET", "/v1/mcp/server")
        if not isinstance(registry, list):
            raise Problem(502, "LiteLLM MCP registry is unavailable")
        matches = [row for row in registry if ALIAS in (row.get("alias"), row.get("server_name"))]
        if not matches:
            return None
        # LiteLLM hides backend URLs from scoped management keys. The registered
        # UUID still binds the alias to the configured URL in that view.
        if (len(matches) != 1 or matches[0].get("url") not in (url, None)
                or any(matches[0].get(k) != v for k, v in spec.items() if k != "url")):
            raise Problem(409, "The switcher_dummy registry entry does not match the configured private endpoint")
        return matches[0]["server_id"]

    registered = await lookup()
    if registered:
        return registered
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
        raise Problem(502, "LiteLLM did not confirm switcher_dummy registration")
    return server_id
