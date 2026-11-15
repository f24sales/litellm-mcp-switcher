import asyncio
import copy
import json
import stat
import uuid
from dataclasses import replace

import httpx
import pytest
from starlette.testclient import TestClient

from mcp_switcher.config import load_configuration
from mcp_switcher.mcp import create_mcp
from mcp_switcher.models import ApplyInput, Problem, Selection
from mcp_switcher.rotation import CredentialStore
from mcp_switcher.service import Service
from test_configuration import configure
from test_orchestrator import call, with_orchestrators
from test_web_mcp import rpc


def rotating_api(service, fake, *, timeout=False, reject=False, unavailable=False):
    original = fake.response
    writes = []

    def response(request):
        if request.url.path == "/key/regenerate":
            data = json.loads(request.content)
            writes.append(data)
            assert set(data) == {"key", "new_key", "grace_period"}
            assert data["grace_period"] == "0s"
            if reject:
                return httpx.Response(403, json={"secret": "never return this"})
            fake.keys[data["new_key"]] = fake.keys.pop(data["key"])
            if timeout:
                raise httpx.ReadTimeout("upstream-secret", request=request)
            return httpx.Response(200, json={"key": data["new_key"]})
        if unavailable and writes and request.url.path == "/key/info":
            return httpx.Response(503)
        return original(request)

    service.upstream.client._transport = httpx.MockTransport(response)
    return writes


async def test_rotation_preserves_grants_is_idempotent_and_survives_restart(setup):
    service, fake, users = setup
    writes = rotating_api(service, fake)
    old, untouched = users[1].litellm_key, copy.deepcopy(fake.keys[users[2].litellm_key])
    expected = copy.deepcopy(fake.keys[old])
    preview = await service.rotation.execute(users[0], "alice", "preview")
    assert not writes and not service.rotation.store.path.exists()
    args = (users[0], "alice", "rotate", preview["revision"], preview["operation_id"])
    result = await service.rotation.execute(*args)
    assert result["status"] == "verified"
    assert await service.rotation.execute(*args) == result
    assert len(writes) == 1
    new = service.users["alice"].litellm_key
    assert old not in fake.keys and new != old
    assert fake.keys[new] == expected and fake.keys[users[2].litellm_key] == untouched
    assert service.target(users[1]).litellm_key == new  # cached login identity uses current key
    assert old not in json.dumps(result) and new not in json.dumps(result)
    assert old not in service.rotation.store.path.read_text()  # no obsolete secret retained
    assert stat.S_IMODE(service.rotation.store.path.stat().st_mode) == 0o600
    restored = service.rotation.store.overlay(users)
    assert restored[1].litellm_key == new and restored[1].bearer == users[1].bearer
    restarted = Service(service.db, service.upstream, restored, service.settings)
    await restarted.rotation.recover()
    assert (await restarted.state(users[1]))["target_key"] == "alice"
    # Unrelated environment edits cannot silently resurrect/replace a credential.
    with pytest.raises(ValueError, match="conflicts"):
        service.rotation.store.overlay([replace(users[1], litellm_key="different-" + "x" * 32)])


@pytest.mark.parametrize("timeout,unavailable", [(True, False), (True, True)])
async def test_lost_response_reconciles_without_second_rotation(setup, timeout, unavailable):
    service, fake, users = setup
    writes = rotating_api(service, fake, timeout=timeout, unavailable=unavailable)
    preview = await service.rotation.execute(users[0], "alice", "preview")
    result = await service.rotation.execute(users[0], "alice", "rotate", preview["revision"], preview["operation_id"])
    assert result["status"] == ("uncertain" if unavailable else "verified")
    # Startup uses the durable candidate even when the old configured token is revoked.
    restored = service.rotation.store.overlay(users)
    restarted = Service(service.db, service.upstream, restored, service.settings)
    rotating_api(restarted, fake)
    await restarted.rotation.recover()
    status = await restarted.rotation.execute(users[0], "alice", "status", operation_id=preview["operation_id"])
    assert status["status"] == "verified" and len(writes) == 1
    assert restarted.users["alice"].litellm_key in fake.keys


async def test_rejected_write_retries_same_candidate_and_blocks_other_operation(setup):
    service, fake, users = setup
    writes = rotating_api(service, fake, reject=True)
    preview = await service.rotation.execute(users[0], "alice", "preview")
    args = (users[0], "alice", "rotate", preview["revision"], preview["operation_id"])
    assert (await service.rotation.execute(*args))["status"] == "retryable"
    with pytest.raises(Problem, match="existing rotation"):
        await service.rotation.execute(users[0], "alice", "rotate", preview["revision"], str(uuid.uuid4()))
    resumed = rotating_api(service, fake)
    assert (await service.rotation.execute(*args))["status"] == "verified"
    assert writes[0]["new_key"] == resumed[0]["new_key"]


async def test_stale_preview_and_unauthorized_calls_do_not_rotate(setup):
    service, fake, users = with_orchestrators(setup)
    writes = rotating_api(service, fake)
    with pytest.raises(Problem, match="requires"):
        await service.rotation.execute(users[1], "alice", "preview")
    with pytest.raises(Problem, match="cannot manage"):
        await service.rotation.execute(users[-1], "bob", "preview")
    preview = await service.rotation.execute(users[-1], "alice", "preview")
    fake.keys[users[1].litellm_key]["models"] = ["changed"]
    with pytest.raises(Problem, match="fresh rotation preview"):
        await service.rotation.execute(users[-1], "alice", "rotate", preview["revision"], preview["operation_id"])
    assert not writes


async def test_rotation_serializes_permission_updates(setup):
    service, fake, users = setup
    rotating_api(service, fake)
    state = await service.state(users[1])
    preview = await service.rotation.execute(users[0], "alice", "preview")
    async with service.key_locks["alice"]:
        pending = asyncio.create_task(service.rotation.execute(users[0], "alice", "rotate",
                                      preview["revision"], preview["operation_id"]))
        await asyncio.sleep(0)
        assert not pending.done()
    assert (await pending)["status"] == "verified"
    await service.apply(users[1], ApplyInput(selection=Selection(servers={"tasks": None}), revision=state["revision"]))
    assert fake.keys[service.users["alice"].litellm_key]["object_permission"]["mcp_servers"] == ["tasks"]


def test_rotation_mcp_visibility_and_cached_call_denial(setup):
    service, fake, users = with_orchestrators(setup)
    writes = rotating_api(service, fake)
    mcp = create_mcp(service, users)
    with TestClient(mcp.http_app(path="/mcp", stateless_http=True, json_response=True)) as client:
        for user in (users[0], users[1], users[-1]):
            _, data = rpc(client, user.bearer, "tools/list")
            assert ("rotate_bearer" in {t["name"] for t in data["result"]["tools"]}) == (user.admin or user.orchestrator)
        for user, key in ((users[1], "alice"), (users[-1], "bob")):
            _, data = rpc(client, user.bearer, "tools/call", {"name": "rotate_bearer", "arguments": {"key_name": key}})
            assert "error" in data or data.get("result", {}).get("isError")
        preview = call(client, users[-1].bearer, "rotate_bearer", key_name="alice")
        result = call(client, users[-1].bearer, "rotate_bearer", key_name="alice", action="rotate",
                      revision=preview["revision"], operation_id=preview["operation_id"])
        assert result["status"] == "verified" and len(writes) == 1


def test_configuration_loads_private_overlay(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    settings, users = load_configuration()
    store = CredentialStore(settings.rotation_credentials_file)
    from mcp_switcher.rotation import digest
    replacement = "sk-" + "n" * 64
    with store.locked():
        store.write({"version": 1, "operations": {}, "keys": {
            "alice": {"key": replacement, "config_hashes": [digest(users[1].litellm_key), digest(replacement)]}}})
    assert load_configuration()[1][1].litellm_key == replacement
    store.path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        load_configuration()
