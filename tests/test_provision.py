import httpx
import pytest

from mcp_switcher.provision import ProvisionError, Provisioner, main


def values(suffix=""):
    return {"LITELLM_MCP_SWITCHER_API_BASE_URL": "http://litellm:4000",
            "LITELLM_MCP_SWITCHER_ADMIN_VIRTUAL_KEY": "admin-" + "a" * 40,
            "LITELLM_MCP_SWITCHER_KEY_NAME" + suffix: "alice", "LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER" + suffix: "a" * 64,
            "LITELLM_MCP_SWITCHER_KEY_ALLOWED_MCPS" + suffix: "tasks, files"}


CATALOG = [{"server_id": "t", "alias": "tasks"}, {"server_id": "f", "alias": "files"}]


def provisioner(handler, config=None, suffix=""):
    return Provisioner(config or values(suffix), "LITELLM_MCP_SWITCHER_KEY_BEARER" + suffix,
                       transport=httpx.MockTransport(handler))


def test_check_is_authenticated_read_only_and_resolves_exact_aliases():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/v1/mcp/server"
        assert request.headers["Authorization"] == "Bearer " + values()["LITELLM_MCP_SWITCHER_ADMIN_VIRTUAL_KEY"]
        return httpx.Response(200, json=CATALOG)
    p = provisioner(handler)
    assert p.check()["mcp_servers"] == ["t", "f"]
    assert len(calls) == 1
    p.client.close()


@pytest.mark.parametrize("status", [301, 401, 403, 500])
def test_non_200_hides_creation_without_leaking_response_secrets(status):
    p = provisioner(lambda r: httpx.Response(status, json={"key": "PRIVATE"}))
    with pytest.raises(ProvisionError, match=f"HTTP {status}") as error:
        p.check()
    assert "PRIVATE" not in str(error.value)
    p.client.close()


@pytest.mark.parametrize("catalog", [[], CATALOG + [{"server_id": "other", "alias": "tasks"}], {}])
def test_unknown_ambiguous_or_invalid_catalog_is_rejected(catalog):
    p = provisioner(lambda r: httpx.Response(200, json=catalog))
    with pytest.raises(ProvisionError):
        p.check()
    p.client.close()


@pytest.mark.parametrize("field,value", [("LITELLM_MCP_SWITCHER_API_BASE_URL", ""),
    ("LITELLM_MCP_SWITCHER_API_BASE_URL", "http://litellm:bad"), ("LITELLM_MCP_SWITCHER_API_BASE_URL", "http://secret@litellm"),
    ("LITELLM_MCP_SWITCHER_ADMIN_VIRTUAL_KEY", ""),
    ("LITELLM_MCP_SWITCHER_KEY_NAME", ""), ("LITELLM_MCP_SWITCHER_KEY_ALLOWED_MCPS", ""), ("LITELLM_MCP_SWITCHER_KEY_ALWAYS_ON_MCPS", "other")])
def test_incomplete_inputs_never_contact_litellm(field, value):
    config = values()
    config[field] = value
    with pytest.raises(ProvisionError):
        provisioner(lambda r: pytest.fail("unexpected request"), config)


def test_new_team_key_and_ceiling_are_isolated_and_mcp_only():
    import json
    requests = []
    team = None
    def handler(request):
        nonlocal team
        requests.append(request)
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/mcp/server":
            return httpx.Response(200, json=CATALOG)
        if request.url.path == "/team/info":
            return httpx.Response(200, json={"team_info": team, "keys": []}) if team else httpx.Response(404)
        if request.url.path == "/team/new":
            team = body
            return httpx.Response(200, json=body)
        if request.url.path == "/team/update":
            return httpx.Response(200, json=body)
        assert request.url.path == "/key/generate"
        assert body["team_id"] == team["team_id"]
        assert body["object_permission"] == team["object_permission"]
        assert body["object_permission"]["mcp_servers"] == ["t", "f"]
        assert body["models"] == ["no-default-models"]
        assert body["allowed_routes"] == ["mcp_inference_routes", "/key/info"]
        return httpx.Response(200, json={"key": "sk-" + "k" * 40})
    config = values("_02")
    config.update({"LITELLM_MCP_SWITCHER_KEY_NAME": "someone-else", "LITELLM_MCP_SWITCHER_KEY_ALLOWED_MCPS": "unrelated"})
    p = provisioner(handler, config, "_02")
    assert p.create() == "sk-" + "k" * 40
    assert team["metadata"]["mcp_switcher_owner"] == "alice"
    assert sum(r.url.path == "/key/generate" for r in requests) == 1
    p.client.close()


@pytest.mark.parametrize("owned,keys", [(False, []), (True, [{"token": "hash"}])])
def test_existing_foreign_team_or_key_is_never_overwritten(owned, keys):
    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/v1/mcp/server":
            return httpx.Response(200, json=CATALOG)
        return httpx.Response(200, json={"team_info": {"metadata": p.marker if owned else {}}, "keys": keys})
    p = provisioner(handler)
    with pytest.raises(ProvisionError):
        p.create()
    p.client.close()


def test_existing_local_bearer_is_reused_without_network(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("LITELLM_MCP_SWITCHER_KEY_BEARER", raising=False)
    path = tmp_path / ".env"
    path.write_text("LITELLM_MCP_SWITCHER_KEY_BEARER=sk-" + "k" * 40 + "\n")
    monkeypatch.setattr(httpx.Client, "request", lambda *_a, **_k: pytest.fail("unexpected request"))
    assert main(["create", "--env-file", str(path), "--config-file", str(tmp_path / "missing")]) == 0
    assert capsys.readouterr().out.strip() == "sk-" + "k" * 40


def test_post_timeout_is_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("private upstream detail")
    p = provisioner(handler)
    with pytest.raises(ProvisionError, match="may have succeeded") as error:
        p.request("POST", "/key/generate", json={})
    assert len(calls) == 1
    assert "private upstream detail" not in str(error.value)
    p.client.close()


def test_managed_key_creation_check_needs_no_personal_switcher_login():
    config = values()
    config.pop("LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER")
    p = provisioner(lambda request: httpx.Response(200, json=CATALOG), config)
    assert p.check()["mcp_servers"] == ["t", "f"]
    p.client.close()
