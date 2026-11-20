from dataclasses import replace

import httpx
import pytest

from mcp_switcher.config import Identity
from mcp_switcher.permissions import migrate, read
from mcp_switcher.startup import prune_configured_access
from mcp_switcher.web import create_web


def test_migration_preserves_other_env_fields_and_existing_edits(tmp_path):
    env = tmp_path / ".env"
    config = tmp_path / "permissions.json"
    env.write_text("LITELLM_MCP_SWITCHER_BEARER_ALIAS_15=agiescape\n"
                   "LITELLM_MCP_SWITCHER_KEY_ALLOWED_MCPS_15=cloak,docs\n"
                   "LITELLM_MCP_SWITCHER_KEY_BEARER_15=test-key\n")
    migrate(env, config, clean=True)
    assert read(config) == {"agiescape": ["cloak", "docs"]}
    assert "ALLOWED_MCPS" not in env.read_text() and "test-key" in env.read_text()
    config.write_text('{"agiescape":["docs"]}')
    migrate(env, config, clean=True)
    assert read(config) == {"agiescape": ["docs"]}
    env.write_text(env.read_text() + "LITELLM_MCP_SWITCHER_KEY_ALLOWED_MCPS_15=cloak\n")
    with pytest.raises(ValueError, match="disagrees"):
        migrate(env, config, clean=True)
    assert "ALLOWED_MCPS" in env.read_text()


async def test_immediate_switch_and_saved_restart_boundary(setup, tmp_path):
    service, upstream, users = setup
    config = tmp_path / "permissions.json"
    config.write_text('{"alice":["docs"],"bob":["docs","tasks"]}')
    service.settings.permissions_file = str(config)
    op = Identity("operator", "user-test-orchestrator", key_names=("alice",))
    app = create_web(service, [*users, op])
    async def restart():
        for name, allowed in read(config).items():
            service.users[name] = replace(service.users[name], allowed_mcps=tuple(allowed))
        await prune_configured_access(service)
    await restart()
    admin = {"Authorization": "Bearer " + users[0].bearer}
    operator = {"Authorization": "Bearer " + op.bearer}
    path = "/api/targets/alice/servers/tasks/active"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        async def state():
            return (await c.get("/api/state?key_name=alice", headers=operator)).json()
        async def toggle(headers, enabled):
            return await c.put(path, headers=headers, json={"revision": (await state())["revision"],
                                                            "enabled": enabled})
        assert len((await state())["servers"]) == 2
        assert (await toggle(operator, True)).status_code == 403
        assert (await toggle(admin, True)).status_code == 200
        assert "tasks" in (await state())["active"]["servers"]
        await restart()
        assert "tasks" not in (await state())["ceiling"]["servers"]
        assert (await toggle(admin, True)).status_code == 200
        assert (await c.put("/api/admin/users/alice/permissions", headers=admin,
                            json={"revision": (await state())["revision"]})).status_code == 200
        await restart()
        assert "tasks" in (await state())["active"]["servers"]
        assert (await toggle(operator, False)).status_code == 200
        after = await state()
        assert "tasks" in after["ceiling"]["servers"] and "tasks" not in after["active"]["servers"]
        assert read(config)["bob"] == ["docs", "tasks"]
