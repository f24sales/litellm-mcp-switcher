# Source of truth: SCRIPTS/githubactions. Generated copies are overwritten.
"""Integration contract against real LiteLLM v1.106.2 on a GitHub runner."""
import asyncio
import json
import os
import threading
import tempfile
from pathlib import Path

import httpx
import uvicorn
from fastmcp import FastMCP
from pydantic import SecretStr

from mcp_switcher.config import Identity, Settings
from mcp_switcher.db import Database
from mcp_switcher.litellm import LiteLLM
from mcp_switcher.mcp import create_mcp
from mcp_switcher.provision import ProvisionError, Provisioner
from mcp_switcher.service import Service


async def provision_contract(req, master, sid):
    """Exercise the setup helper against LiteLLM, without production credentials."""
    values = {"LITELLM_MCP_SWITCHER_API_BASE_URL": "http://127.0.0.1:4000", "LITELLM_MASTER_KEY": master,
              "LITELLM_MCP_SWITCHER_KEY_NAME_02": "setup-contract", "LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER_02": "s" * 64,
              "LITELLM_MCP_SWITCHER_KEY_ALLOWED_MCPS_02": "contract"}
    helper = Provisioner(values, "LITELLM_MCP_SWITCHER_KEY_BEARER_02")
    try:
        permission = await asyncio.to_thread(helper.check)
        assert permission["mcp_servers"] == [sid]
        key = await asyncio.to_thread(helper.create)
        settings = Settings(litellm_admin_virtual_key=SecretStr(master),
                            litellm_api_base_url=values["LITELLM_MCP_SWITCHER_API_BASE_URL"])
        upstream = LiteLLM(settings)
        try:
            identity = Identity("setup-contract", "s" * 64, key)
            info, team, ceiling, active = await upstream.scope(identity, await upstream.inventory())
            assert info["team_id"] == team["team_id"] == helper.team_id
            assert ceiling.servers == active.servers == {sid: None}
            assert info["models"] == ["no-default-models"]
        finally:
            await upstream.close()
        tools = await req("GET", "/mcp-rest/tools/list?server_id=" + sid, key=key)
        assert {t["name"] for t in tools["tools"]} == {"read", "write"}
        await req("POST", "/v1/chat/completions", {"model": "none", "messages": []}, key=key, expected=403)
        try:
            await asyncio.to_thread(helper.create)
        except ProvisionError as error:
            assert "already has a key" in str(error)
        else:
            raise AssertionError("Repeated setup must not create a second key")
        assert len((await req("GET", "/team/info?team_id=" + helper.team_id))["keys"]) == 1
    finally:
        helper.client.close()
    print("Setup contract: dedicated team, exact MCP ceiling, callable MCP-only key, repeat protection passed")


async def always_on_contract(req, master, key, optional_id):
    """Call the real Switcher through LiteLLM, including after clearing every optional MCP."""
    settings = Settings(litellm_admin_virtual_key=SecretStr(master),
                        litellm_api_base_url="http://127.0.0.1:4000")
    admin = Identity("admin", master, admin=True)
    user = Identity("contract", "ci-switcher-" + "s" * 40, key,
                    always_on_mcps=("switcher_contract",))
    with tempfile.TemporaryDirectory() as directory:
        db = Database("sqlite:///" + str(Path(directory) / "switcher.sqlite3"), "contract")
        db.migrate()
        db.initialize_owners([user.name])
        upstream = LiteLLM(settings)
        service = Service(db, upstream, [admin, user], settings)
        mcp = create_mcp(service, [admin, user])
        server = uvicorn.Server(uvicorn.Config(mcp.http_app(path="/mcp", stateless_http=True),
            host="127.0.0.1", port=8998, log_level="error"))
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                if task.done():
                    task.result()
                    raise AssertionError("Switcher MCP stopped before becoming ready")
                await asyncio.sleep(.1)
            assert server.started
            registered = await req("POST", "/v1/mcp/server", {
                "server_name": "switcher_contract", "alias": "switcher_contract",
                "url": "http://127.0.0.1:8998/mcp", "transport": "http", "auth_type": "bearer_token",
                "credentials": {"auth_value": user.bearer}, "allow_all_keys": False,
                "available_on_public_internet": False}, expected=(200, 201))
            control_id = registered["server_id"]
            permission = {"mcp_servers": [optional_id, control_id]}
            await req("POST", "/team/update", {"team_id": "contract", "object_permission": permission})
            await req("POST", "/key/update", {"key": key, "object_permission": permission,
                "allowed_routes": ["mcp_inference_routes", "/key/info"]})
            for selection in ({"servers": {}}, {"servers": {control_id: ["current_state"]}}):
                state = await service.state(user)
                result = await req("POST", "/mcp-rest/tools/call", {"server_id": control_id,
                    "name": "apply_selection", "arguments": {"selection": selection,
                    "revision": state["revision"]}}, key=key)
                assert not result.get("isError"), result
                state = await service.state(user)
                assert state["active"] == {"servers": {control_id: None}}, state
                assert state["operation"]["status"] == "verified"
                tools = await req("GET", "/mcp-rest/tools/list?server_id=" + control_id, key=key)
                assert {t["name"] for t in tools["tools"]} == {
                    "current_state", "discover_tools", "refresh_tools", "save_project", "delete_project", "apply_selection"}
                for tool in tools["tools"]:
                    if tool["name"] in {"save_project", "delete_project", "apply_selection"}:
                        assert "USER AUTHORIZATION REQUIRED" in tool["description"]
                await req("GET", "/mcp-rest/tools/list?server_id=" + optional_id, key=key, expected=403)
                result = await req("POST", "/mcp-rest/tools/call", {"server_id": control_id,
                    "name": "current_state", "arguments": {}}, key=key)
                assert not result.get("isError"), result
            print("Always-on contract: real user Switcher tools remain callable through LiteLLM after clear-all and tool restriction")
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=15)
            await upstream.close()
            db.close()


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
        await provision_contract(req, master, sid)
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
        await always_on_contract(req, master, key, sid)
    server.should_exit = True
    thread.join(timeout=10)
    print("Real LiteLLM contract: explicit opt-out, tool restriction, cached-call denial, team ceiling, LLM denial passed")


asyncio.run(main())
