import json

import httpx
from starlette.testclient import TestClient

from mcp_switcher.mcp import create_mcp
from mcp_switcher.web import create_web


async def test_web_auth_isolation_and_injected_user_ids(setup):
    service, fake, users = setup
    app = create_web(service, users)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/state")).status_code == 401
        assert (await client.get("/api/state", headers={"Authorization": "Bearer unknown"})).status_code == 401
        headers = {"Authorization": "Bearer " + users[1].bearer}
        assert (await client.get("/api/me", headers=headers)).json()["name"] == "alice"
        assert (await client.get("/api/admin/users", headers=headers)).status_code == 403
        assert (await client.get("/api/admin/users/bob", headers=headers)).status_code == 403
        body = {"name": "Bad", "selection": {"servers": {}}, "user_id": "bob"}
        assert (await client.post("/api/projects", headers=headers, json=body)).status_code == 422
        response = await client.get("/")
        assert response.status_code == 200
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert "no-store" in response.headers["Cache-Control"]
        for user in users:
            assert user.bearer not in response.text
            assert user.litellm_key not in response.text or not user.litellm_key


def rpc(client, token, method, params=None, ident=1):
    response = client.post("/mcp", headers={"Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": ident,
        "method": method, **({"params": params} if params is not None else {})})
    if "text/event-stream" in response.headers.get("content-type", ""):
        messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        return response.status_code, messages[-1]
    return response.status_code, response.json()


def test_mcp_http_auth_catalog_and_cached_admin_call(setup):
    service, fake, users = setup
    mcp = create_mcp(service, users)
    app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        status, _ = rpc(client, "unknown", "tools/list")
        assert status == 401
        init = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}
        assert rpc(client, users[1].bearer, "initialize", init)[0] == 200
        status, data = rpc(client, users[1].bearer, "tools/list")
        assert status == 200
        names = {t["name"] for t in data["result"]["tools"]}
        assert "current_state" in names and "admin_set_access" not in names
        assert "refresh_tools" in names
        for tool in data["result"]["tools"]:
            readonly = tool["name"] in {"current_state", "discover_tools"}
            assert tool["annotations"]["readOnlyHint"] is readonly
            if not readonly:
                assert "USER AUTHORIZATION REQUIRED" in tool["description"]
                assert "Never modify settings on your own initiative" in tool["description"]
        status, data = rpc(client, users[0].bearer, "tools/list")
        names = {t["name"] for t in data["result"]["tools"]}
        assert "admin_set_access" in names and "apply_selection" not in names
        admin_write = next(t for t in data["result"]["tools"] if t["name"] == "admin_set_access")
        assert "USER AUTHORIZATION REQUIRED" in admin_write["description"]
        assert admin_write["annotations"]["readOnlyHint"] is False
        status, data = rpc(client, users[1].bearer, "tools/call", {"name": "admin_list_users", "arguments": {}})
        assert "error" in data or data.get("result", {}).get("isError")
        status, data = rpc(client, users[0].bearer, "tools/call", {"name": "admin_list_users", "arguments": {}})
        assert not data.get("result", {}).get("isError")
        assert "alice" in json.dumps(data) and users[1].bearer not in json.dumps(data)
