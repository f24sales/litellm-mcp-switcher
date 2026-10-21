import pytest

from mcp_switcher.config import authenticate, load_configuration


def configure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LITELLM_ADMIN_VIRTUAL_KEY", "admin-" + "a" * 40)
    monkeypatch.setenv("USER_NAME", "alice")
    monkeypatch.setenv("USER_SWITCHER_BEARER", "switcher-" + "s" * 40)
    monkeypatch.setenv("USER_LITELLM_BEARER", "litellm-" + "l" * 40)


def test_repeat_groups_and_blank_sqlite_fields(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    for key in ("HOST", "PORT", "NAME", "USER", "PW", "PREFIX"):
        monkeypatch.setenv("MCP_SWITCHER_DB_" + key, "")
    monkeypatch.setenv("USER_NAME_02", "bob")
    monkeypatch.setenv("USER_SWITCHER_BEARER_02", "bob-switcher-" + "s" * 40)
    monkeypatch.setenv("USER_LITELLM_BEARER_02", "bob-litellm-" + "l" * 40)
    settings, users = load_configuration()
    assert [u.name for u in users] == ["admin", "alice", "bob"]
    assert settings.mcp_switcher_db_port == 5432
    assert authenticate("not-configured", users) is None
    assert authenticate(users[1].bearer, users).name == "alice"
    assert authenticate(users[1].litellm_key, users) is None


@pytest.mark.parametrize("variable,value", [
    ("USER_LITELLM_BEARER", ""), ("USER_NAME", "admin"),
    ("USER_SWITCHER_BEARER", "admin-" + "a" * 40),
])
def test_invalid_groups_fail(monkeypatch, tmp_path, variable, value):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError):
        load_configuration()


@pytest.mark.parametrize("web,mcp", [("1", "0"), ("0", "1"), ("1", "1")])
def test_service_enablement(monkeypatch, tmp_path, web, mcp):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("WEBUI_ENABLED", web)
    monkeypatch.setenv("MCP_ENABLED", mcp)
    settings, _ = load_configuration()
    assert settings.webui_enabled == (web == "1")
    assert settings.mcp_enabled == (mcp == "1")


def test_both_listeners_disabled_is_rejected(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv("WEBUI_ENABLED", "0")
    monkeypatch.setenv("MCP_ENABLED", "0")
    with pytest.raises(ValueError):
        load_configuration()
