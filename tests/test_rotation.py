import asyncio
import copy
import hashlib
import json
import secrets
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
    for info in fake.keys.values():
        info["budget_id"] = None
        info.setdefault("key_alias", "fixture-" + info["team_id"])
        info.setdefault("rpm_limit", 17)

    def response(request):
        if request.url.path == "/key/generate":
            data = json.loads(request.content)
            writes.append(data)
            if reject:
                return httpx.Response(403, json={"secret": "never return this"})
            source = next(info for info in fake.keys.values() if info["team_id"] == data["team_id"])
            key = data.get("key") or "sk-" + secrets.token_urlsafe(16)
            fake.keys[key] = {**copy.deepcopy(source), "key_alias": data["key_alias"]}
            if timeout:
                raise httpx.ReadTimeout("upstream-secret", request=request)
            return httpx.Response(200, json={"key": key})
        if request.url.path == "/team/info":
            team = request.url.params["team_id"]
            return httpx.Response(200, json={"team_info": fake.teams[team], "keys": [
                {**v, "token": hashlib.sha256(k.encode()).hexdigest()} for k, v in fake.keys.items()
                if v["team_id"] == team]})
        if request.url.path == "/key/delete":
            for key in json.loads(request.content)["keys"]:
                stored = next((k for k in fake.keys if hashlib.sha256(k.encode()).hexdigest() == key), key)
                fake.keys.pop(stored, None)
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/key/update" and "key_alias" in json.loads(request.content):
            data = json.loads(request.content)
            fake.keys[data["key"]]["key_alias"] = data["key_alias"]
            return httpx.Response(200, json={"ok": True})
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
    assert "key" not in writes[0] and len(new) == 25
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
async def test_lost_generate_response_preserves_old_key_and_retries_safely(setup, timeout, unavailable):
    service, fake, users = setup
    writes = rotating_api(service, fake, timeout=timeout, unavailable=unavailable)
    preview = await service.rotation.execute(users[0], "alice", "preview")
    result = await service.rotation.execute(users[0], "alice", "rotate", preview["revision"], preview["operation_id"])
    assert result["status"] == ("uncertain" if unavailable else "retryable")
    assert users[1].litellm_key in fake.keys
    # Startup removes only the unpublished key. An explicit retry requests a new one.
    restored = service.rotation.store.overlay(users)
    restarted = Service(service.db, service.upstream, restored, service.settings)
    rotating_api(restarted, fake)
    await restarted.rotation.recover()
    status = await restarted.rotation.execute(users[0], "alice", "status", operation_id=preview["operation_id"])
    assert status["status"] == "retryable" and len(writes) == 1
    assert set(fake.keys) == {users[1].litellm_key, users[2].litellm_key}
    retried = await restarted.rotation.execute(users[0], "alice", "rotate", preview["revision"], preview["operation_id"])
    assert retried["status"] == "verified" and restarted.users["alice"].litellm_key in fake.keys


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
    assert writes[0] == resumed[0] and "key" not in writes[0]


async def test_resume_rotation_journaled_before_server_generated_keys(setup):
    service, fake, users = setup
    writes = rotating_api(service, fake, reject=True)
    preview = await service.rotation.execute(users[0], "alice", "preview")
    args = (users[0], "alice", "rotate", preview["revision"], preview["operation_id"])
    assert (await service.rotation.execute(*args))["status"] == "retryable"
    journal = service.rotation.store.read()
    op = journal["operations"][preview["operation_id"]]
    op.pop("candidate_alias")
    op["new_key"] = "sk-" + "a" * 64
    service.rotation.store.write(journal)
    resumed = rotating_api(service, fake)
    assert (await service.rotation.execute(*args))["status"] == "verified"
    assert len(writes) == len(resumed) == 1
    assert service.users["alice"].litellm_key == resumed[0]["key"] == op["new_key"]


@pytest.mark.parametrize("lost_path", ["/key/delete", "/key/update"])
async def test_revocation_or_alias_response_loss_resumes_without_recreating_key(setup, lost_path):
    service, fake, users = setup
    writes = rotating_api(service, fake)
    transport = service.upstream.client._transport
    failed = False

    async def response(request):
        nonlocal failed
        result = await transport.handle_async_request(request)
        if request.url.path == lost_path and not failed:
            failed = True
            raise httpx.ReadTimeout("lost write response", request=request)
        return result

    service.upstream.client._transport = httpx.MockTransport(response)
    preview = await service.rotation.execute(users[0], "alice", "preview")
    result = await service.rotation.execute(users[0], "alice", "rotate", preview["revision"], preview["operation_id"])
    assert result["status"] == "uncertain" and failed
    restarted = Service(service.db, service.upstream, service.rotation.store.overlay(users), service.settings)
    await restarted.rotation.recover()
    status = await restarted.rotation.execute(users[0], "alice", "status", operation_id=preview["operation_id"])
    assert status["status"] == "verified" and len(writes) == 1
    assert users[1].litellm_key not in fake.keys
    assert fake.keys[restarted.users["alice"].litellm_key]["key_alias"] == "fixture-alice"


async def test_stale_preview_and_unauthorized_calls_do_not_rotate(setup):
    service, fake, users = with_orchestrators(setup)
    writes = rotating_api(service, fake)
    with pytest.raises(Problem, match="requires"):
        await service.rotation.execute(users[1], "alice", "preview")
    with pytest.raises(Problem, match="cannot manage"):
        await service.rotation.execute(users[-1], "bob", "preview")
    preview = await service.rotation.execute(users[-1], "alice", "preview")
    fake.keys[users[1].litellm_key]["rpm_limit"] = 23
    with pytest.raises(Problem, match="fresh rotation preview"):
        await service.rotation.execute(users[-1], "alice", "rotate", preview["revision"], preview["operation_id"])
    assert not writes


@pytest.mark.parametrize("field,value", [("models", ["some-llm"]), ("spend", 1), ("expires", "2030-01-01"),
                                         ("budget_id", "shared-budget"), ("budget_duration", "1d")])
async def test_accounting_and_expiring_keys_are_rejected_before_writing(setup, field, value):
    service, fake, users = setup
    writes = rotating_api(service, fake)
    fake.keys[users[1].litellm_key][field] = value
    with pytest.raises(Problem, match="MCP-only"):
        await service.rotation.execute(users[0], "alice", "preview")
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
