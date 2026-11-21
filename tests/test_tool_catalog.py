import asyncio
import copy

from mcp_switcher.models import Problem


async def test_shared_catalog_refresh_and_per_alias_filtering(setup, monkeypatch):
    service, fake, (admin, alice, bob) = setup
    catalog = service.tool_catalog
    reads = []
    tools = [{"name": "read", "description": "Read"}, {"name": "write", "description": "Write"}]

    async def discover(sid):
        reads.append(sid)
        await asyncio.sleep(0)
        if sid == "docs":
            raise Problem(502, "Discovery failed")
        return list(tools)

    monkeypatch.setattr(service.upstream, "tools", discover)
    first = catalog.refresh_all()
    assert catalog.refresh_all() is first
    await first
    assert sorted(reads) == sorted(row["server_id"] for row in fake.registry)
    assert catalog.entries["docs"]["status"] == "error"
    scope = {"mcp_servers": ["tasks"], "mcp_tool_permissions": {"tasks": ["read"]}}
    fake.teams[bob.name]["object_permission"] = copy.deepcopy(scope)
    fake.keys[bob.litellm_key]["object_permission"] = copy.deepcopy(scope)
    initial_count = len(reads)
    full = await service.tools(admin, "tasks", alice.name)
    limited = await service.tools(bob, "tasks")
    assert [t["name"] for t in limited["tools"]] == ["read"]
    assert full["tools"] == tools and full["revision"] == limited["revision"]
    assert len(reads) == initial_count
    tools.append({"name": "list", "description": "List"})
    updated = await asyncio.gather(catalog.get("tasks", refresh=True), catalog.get("tasks", refresh=True))
    assert len(reads) == initial_count + 1 and updated[0] == updated[1]
    assert updated[0]["revision"] > full["revision"]
    state = await service.state(bob)
    row = next(s for s in state["servers"] if s["server_id"] == "tasks")
    assert row["tool_status"] == "reachable" and row["tool_revision"] == updated[0]["revision"]
    assert (await service.tools(admin, "tasks", alice.name))["tools"] == tools
    assert [t["name"] for t in (await service.tools(bob, "tasks"))["tools"]] == ["read"]
    assert len(reads) == initial_count + 1
