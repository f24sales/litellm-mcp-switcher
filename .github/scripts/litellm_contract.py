"""Integration contract against real LiteLLM v1.106.2 on a GitHub runner."""
import asyncio
import json
import os
import threading
import time

import httpx
import uvicorn
from fastmcp import FastMCP


async def main():
    mcp = FastMCP("Contract fixture")

    @mcp.tool
    def read() -> str:
        """Read deterministic test data."""
        return "fixture"

    @mcp.tool
    def write() -> str:
        """A harmless fixture used to test a denied cached tool invocation."""
        return "must not be callable after restriction"

    server = uvicorn.Server(uvicorn.Config(mcp.http_app(path="/mcp", stateless_http=True),
                                          host="127.0.0.1", port=8999, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    master = os.environ["CONTRACT_MASTER_KEY"]
    async with httpx.AsyncClient(base_url="http://127.0.0.1:4000", timeout=60) as client:
        async def req(method, path, data=None, key=master, expected=200):
            response = await client.request(method, path, json=data,
                headers={"Authorization": "Bearer " + key})
            assert response.status_code in (expected if isinstance(expected, tuple) else (expected,)), (
                path, response.status_code, response.text[:400])
            return response.json()
        for _ in range(120):
            try:
                if (await client.get("/health/liveliness")).status_code == 200 and server.started:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
        else:
            raise AssertionError("Contract services did not become ready")
        registered = await req("POST", "/v1/mcp/server", {"server_name": "contract", "alias": "contract",
            "url": "http://127.0.0.1:8999/mcp", "transport": "http", "allow_all_keys": False,
            "available_on_public_internet": False}, expected=(200, 201))
        sid = registered["server_id"]
        await req("POST", "/team/new", {"team_id": "contract", "object_permission": {"mcp_servers": [sid]}})
        result = await req("POST", "/key/generate", {"team_id": "contract", "models": ["no-default-models"],
            "allowed_routes": ["mcp_inference_routes"], "object_permission": {"mcp_servers": [sid]}})
        key = result["key"]
        tools = await req("GET", "/mcp-rest/tools/list?server_id=" + sid, key=key)
        assert {t["name"] for t in tools["tools"]} == {"read", "write"}, tools
        await req("POST", "/key/update", {"key": key, "object_permission": {"mcp_servers": ["no-mcp-servers"]}})
        await req("GET", "/mcp-rest/tools/list?server_id=" + sid, key=key, expected=403)
        await req("POST", "/key/update", {"key": key, "object_permission": {
            "mcp_servers": [sid], "mcp_tool_permissions": {sid: ["read"]}}})
        tools = await req("GET", "/mcp-rest/tools/list?server_id=" + sid, key=key)
        assert [t["name"] for t in tools["tools"]] == ["read"], tools
        await req("POST", "/mcp-rest/tools/call", {"server_id": sid, "name": "write", "arguments": {}},
                  key=key, expected=(400, 403))
        allowed = await req("POST", "/mcp-rest/tools/call", {"server_id": sid, "name": "read", "arguments": {}}, key=key)
        assert "fixture" in json.dumps(allowed)
        await req("POST", "/team/update", {"team_id": "contract", "object_permission": {
            "mcp_servers": ["no-mcp-servers"]}})
        await req("GET", "/mcp-rest/tools/list?server_id=" + sid, key=key, expected=403)
        await req("POST", "/v1/chat/completions", {"model": "none", "messages": []}, key=key, expected=403)
    server.should_exit = True
    thread.join(timeout=10)
    print("Real LiteLLM contract: explicit opt-out, tool restriction, cached-call denial, team ceiling, LLM denial passed")


asyncio.run(main())
