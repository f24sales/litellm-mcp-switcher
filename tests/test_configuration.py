import pytest

from mcp_switcher.config import authenticate, load_configuration


def configure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_ADMIN_VIRTUAL_KEY", "admin-" + "a" * 40)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_BEARER_ALIAS", "alice")
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER", "switcher-" + "s" * 40)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_BEARER", "litellm-" + "l" * 40)


def test_repeat_groups_and_blank_sqlite_fields(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    for key in ("HOST", "PORT", "NAME", "USER", "PW", "PREFIX"):
        monkeypatch.setenv("LITELLM_MCP_SWITCHER_DB_" + key, "")
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_BEARER_ALIAS_02", "bob")
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER_02", "bob-switcher-" + "s" * 40)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_BEARER_02", "bob-litellm-" + "l" * 40)
    settings, users = load_configuration()
    assert [u.name for u in users] == ["admin", "alice", "bob"]
    assert settings.mcp_switcher_db_port == 5432
    assert authenticate("not-configured", users) is None
    assert authenticate(users[1].bearer, users).name == "alice"
    assert authenticate(users[1].litellm_key, users) is None


@pytest.mark.parametrize("variable,value", [
    ("LITELLM_MCP_SWITCHER_KEY_BEARER", ""), ("LITELLM_MCP_SWITCHER_BEARER_ALIAS", "admin"),
    ("LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER", "admin-" + "a" * 40),
])
def test_invalid_groups_fail(monkeypatch, tmp_path, variable, value):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError):
        load_configuration()


@pytest.mark.parametrize("web,mcp", [("1", "0"), ("0", "1"), ("1", "1")])
def test_service_enablement(monkeypatch, tmp_path, web, mcp):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_WEBUI_ENABLED", web)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_MCP_ENABLED", mcp)
    settings, _ = load_configuration()
    assert settings.webui_enabled == (web == "1")
    assert settings.mcp_enabled == (mcp == "1")


def test_both_listeners_disabled_is_rejected(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_WEBUI_ENABLED", "0")
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_MCP_ENABLED", "0")
    with pytest.raises(ValueError):
        load_configuration()



def test_cross_user_bearer_role_collision_is_rejected(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_BEARER_ALIAS_02", "bob")
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER_02", "litellm-" + "l" * 40)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_BEARER_02", "bob-litellm-" + "l" * 40)
    with pytest.raises(ValueError, match="namespaces"):
        load_configuration()


def test_always_on_aliases_belong_to_each_repeat_group(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_ALWAYS_ON_MCPS", "switcher_alice, other_control")
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_BEARER_ALIAS_02", "bob")
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_SWITCHER_BEARER_02", "bob-switcher-" + "s" * 40)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_BEARER_02", "bob-litellm-" + "l" * 40)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_ALWAYS_ON_MCPS_02", "switcher_bob")
    _, users = load_configuration()
    assert users[1].always_on_mcps == ("switcher_alice", "other_control")
    assert users[2].always_on_mcps == ("switcher_bob",)


@pytest.mark.parametrize("aliases", ["duplicate,duplicate", "not an alias", "https://example.com/mcp"])
def test_invalid_always_on_aliases_rejected(monkeypatch, tmp_path, aliases):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("LITELLM_MCP_SWITCHER_KEY_ALWAYS_ON_MCPS", aliases)
    with pytest.raises(ValueError, match="aliases"):
        load_configuration()
