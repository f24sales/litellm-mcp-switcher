import hashlib
import json
import secrets
import uuid

import httpx
import pytest
from mcp_switcher.config import load_configuration
from mcp_switcher.models import ProjectInput, Selection
from mcp_switcher.web import create_web


@pytest.mark.parametrize("dummy_enabled", [True, False])
async def test_aliases_logins_reveal_and_restart(setup, tmp_path, monkeypatch, dummy_enabled):
    service, fake, identities = setup
    service.settings.dummy_mcp = dummy_enabled
    admin, alice, bob = identities
    from mcp_switcher.config import Identity
    op = Identity("operator", "operator-" + "s" * 32, key_names=("alice", "bob"))
    identities.append(op)
    prefix = "LITELLM_MCP_SWITCHER_"
    settings = {
        "ADMIN_VIRTUAL_KEY": admin.bearer, "BEARER_ALIAS": alice.name, "KEY_BEARER": alice.litellm_key,
        "KEY_SWITCHER_BEARER": alice.bearer, "BEARER_ALIAS_02": bob.name, "KEY_BEARER_02": bob.litellm_key,
        "KEY_SWITCHER_BEARER_02": bob.bearer, "ORCHESTRATOR_NAME": op.name, "ORCHESTRATOR_BEARER": op.bearer,
        "ORCHESTRATOR_BEARER_ALIASES": "alice,bob", "PERMISSIONS_FILE": str(tmp_path / "permissions.json"),
        "ROTATION_CREDENTIALS_FILE": service.settings.rotation_credentials_file,
    }
    for k, v in settings.items():
        monkeypatch.setenv(prefix + k, v)
    service.settings.permissions_file = settings["PERMISSIONS_FILE"]
    (tmp_path / "permissions.json").write_text('{"alice":["docs"], "bob":[]}')
    service.db.save_project("alice", ProjectInput(name="report", selection=Selection()))

    lose_response = True

    def upstream(req):
        nonlocal lose_response
        body = json.loads(req.content) if req.content else {}
        path = req.url.path
        if path == "/key/info" and "key" in req.url.params:
            info = next((v for k, v in fake.keys.items() if hashlib.sha256(k.encode()).hexdigest()
                         == req.url.params["key"]), None)
            return httpx.Response(200, json={"info": info}) if info else httpx.Response(404)
        if path == "/v1/mcp/server" and req.method == "POST":
            assert not any(r.get("alias") == "switcher-dummy" for r in fake.registry)
            fake.registry.append({**body, "url": None})
            return httpx.Response(201, json={"server_id": body["server_id"]})
        if path == "/team/new":
            fake.teams[body["team_id"]] = body
        elif path == "/team/info":
            team = req.url.params["team_id"]
            return httpx.Response(200, json={"team_info": fake.teams[team],
                                            "keys": [{**v, "token": hashlib.sha256(k.encode()).hexdigest()}
                                                     for k, v in fake.keys.items() if v["team_id"] == team]})
        elif path == "/key/generate":
            assert "key" not in body
            key = "sk-" + secrets.token_hex(32)
            fake.keys[key] = body
            if lose_response:
                lose_response = False
                raise httpx.ReadTimeout("Lost key generation reply")
            return httpx.Response(200, json={"key": key})
        elif path == "/key/delete":
            for key in body["keys"]:
                stored = next((k for k in fake.keys if hashlib.sha256(k.encode()).hexdigest() == key), key)
                fake.keys.pop(stored)
        elif path == "/key/update" and "key_alias" in body:
            fake.keys[body["key"]]["key_alias"] = body["key_alias"]
        else:
            return fake.response(req)
        return httpx.Response(200, json={})

    service.upstream.client._transport = httpx.MockTransport(upstream)
    root = "/api/admin/users"

    def auth(key):
        return {"Authorization": "Bearer " + key}

    def restarted():
        return {u.name: u for u in load_configuration()[1]}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_web(service, identities)),
                                 base_url="http://test", headers=auth(admin.bearer)) as c:
        async def rev(name):
            return {"revision": (await c.get(root)).json()["revisions"][name]}

        for token in (bob.bearer, op.bearer):
            assert (await c.post(root + "/alice/credentials/virtual", headers=auth(token))).status_code == 403
            assert (await c.post(root, json={"name": "new"}, headers=auth(token))).status_code == 403
        assert (await c.post(root + "/admin/credentials/virtual")).status_code == 404
        assert (await c.post(root, json={"name": "client"})).status_code == 502
        assert "client" not in restarted()
        assert (await c.post(root, json={"name": "client"})).status_code == 200
        assert sum(v.get("key_alias") == "mcp-switcher-client" for v in fake.keys.values()) == 1
        assert (await c.post(root, json={"name": "client"})).status_code == 409
        created = restarted()["client"]
        assert created.litellm_key in fake.keys and not created.bearer
        assert (await c.post(root + "/client/credentials/switcher")).status_code == 404
        assert (await c.post(root + "/client/login", params=await rev("client"))).status_code == 200
        assert restarted()["client"].litellm_key == created.litellm_key
        for kind in ("switcher", "virtual"):
            r = await c.post(root + "/client/credentials/" + kind)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
            value = r.json()["value"]
            assert value not in (await c.get(root)).text
            if kind == "virtual":
                server_id = str(uuid.uuid5(uuid.NAMESPACE_URL, service.settings.dummy_mcp_url))
                expected = [server_id] if dummy_enabled else ["no-mcp-servers"]
                assert fake.keys[value]["object_permission"]["mcp_servers"] == expected
                assert fake.teams[fake.keys[value]["team_id"]]["object_permission"]["mcp_servers"] == expected
                assert restarted()["client"].allowed_mcps == (("switcher-dummy",) if dummy_enabled else ())
        assert restarted()["client"] == service.users["client"]
        assert (await c.put(root + "/alice/name", params=await rev("alice"), json={"name": "renamed"})).status_code == 200
        assert restarted()["operator"].key_names == ("renamed", "bob")
        assert restarted()["renamed"].owner == "alice"
        assert (await c.get(root + "/renamed")).json()["projects"][0]["name"] == "report"
        assert (await c.get("/api/me", headers=auth(alice.bearer))).json()["name"] == "renamed"
        old_rev = await rev("renamed")
        assert (await c.post(root + "/renamed/login", params=old_rev)).status_code == 200
        assert (await c.post(root + "/renamed/login", params=old_rev)).status_code == 409
        assert (await c.get("/api/me", headers=auth(alice.bearer))).status_code == 401
        new_login = (await c.post(root + "/renamed/credentials/switcher")).json()["value"]
        assert restarted()["renamed"].bearer == new_login
        login_path = root + "/renamed/login"
        for token in (bob.bearer, op.bearer):
            assert (await c.delete(login_path, params=await rev("renamed"), headers=auth(token))).status_code == 403
        assert (await c.delete(login_path, params=old_rev)).status_code == 409
        assert (await c.delete(login_path, params=await rev("renamed"))).status_code == 200
        assert (await c.get("/api/me", headers=auth(new_login))).status_code == 401
        assert (await c.post(root + "/renamed/credentials/switcher")).status_code == 404
        assert (await c.post(root + "/renamed/credentials/virtual")).json()["value"] == alice.litellm_key
        assert restarted()["renamed"].bearer == "" and restarted()["renamed"].litellm_key == alice.litellm_key
        assert restarted()["operator"].key_names == ("renamed", "bob")
        assert (await c.get(root + "/renamed")).json()["projects"][0]["name"] == "report"
        assert (await c.post(login_path, params=await rev("renamed"))).status_code == 200
        assert restarted()["renamed"].bearer not in ("", new_login)
        assert (await c.delete(root + "/renamed", params=await rev("renamed"))).status_code == 200
        assert alice.litellm_key not in fake.keys and bob.litellm_key in fake.keys
        assert (await c.get("/api/me", headers=auth(new_login))).status_code == 401
        assert "renamed" not in restarted() and "alice" not in restarted()
        assert restarted()["operator"].key_names == ("bob",)
        assert not service.db.list_projects("alice")
        assert (await c.post(root, json={"name": "alice"})).status_code == 200
        assert restarted()["alice"].owner != "alice"
        assert restarted()["operator"].key_names == ("bob",)
