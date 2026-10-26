import copy
from dataclasses import replace

import pytest

from mcp_switcher.litellm import DENY
from mcp_switcher.models import Problem, ProjectInput, Selection
from mcp_switcher.startup import prune_configured_access


async def test_startup_removes_references_including_inactive_and_presets_only_for_managed_key(setup):
    service, fake, (_, original, bob) = setup
    alice = replace(original, allowed_mcps=("tasks",))
    service.users = {alice.name: alice}  # Bob is no longer managed; leave his access and presets untouched.
    fake.keys[alice.litellm_key]["object_permission"].update(
        mcp_servers=["tasks"], mcp_tool_permissions={"tasks": ["read"]})
    fake.teams[alice.name]["object_permission"]["mcp_tool_permissions"] = {"tasks": ["read"]}
    project = ProjectInput(name="Both", selection=Selection(servers={"tasks": ["read"], "docs": None}))
    service.db.save_project(alice.name, project)
    service.db.save_project(bob.name, project)
    untouched = copy.deepcopy((fake.keys[bob.litellm_key], fake.teams[bob.name],
                               service.db.list_projects(bob.name), fake.registry))
    fake.fail_tools = True  # Removing references needs no upstream tool discovery.
    await prune_configured_access(service)
    assert fake.keys[alice.litellm_key]["object_permission"]["mcp_tool_permissions"] == {"tasks": ["read"]}
    assert fake.teams[alice.name]["object_permission"]["mcp_servers"] == ["tasks"]
    assert fake.keys[alice.litellm_key]["budget_id"] == "unchanged"
    assert fake.teams[alice.name]["object_permission"]["vector_stores"] == ["keep-me"]
    saved = service.db.list_projects(alice.name)[0]
    assert saved["selection"] == {"servers": {"tasks": ["read"]}} and saved["version"] == 2
    assert (fake.keys[bob.litellm_key], fake.teams[bob.name], service.db.list_projects(bob.name), fake.registry) == untouched
    writes = copy.deepcopy(fake.writes)
    await prune_configured_access(service)
    assert fake.writes == writes and service.db.list_projects(alice.name)[0] == saved


async def test_startup_blank_is_explicit_deny_and_allowed_does_not_add_or_enable(setup):
    service, fake, (_, alice, bob) = setup
    service.users[bob.name] = replace(bob, allowed_mcps=("tasks", "docs"))
    fake.teams[bob.name]["object_permission"]["mcp_servers"] = ["tasks"]
    fake.keys[bob.litellm_key]["object_permission"]["mcp_servers"] = [DENY]
    await prune_configured_access(service)
    for permission in (fake.keys[alice.litellm_key]["object_permission"], fake.teams[alice.name]["object_permission"]):
        assert permission["mcp_servers"] == [DENY]
    assert fake.teams[bob.name]["object_permission"]["mcp_servers"] == ["tasks"]
    assert fake.keys[bob.litellm_key]["object_permission"]["mcp_servers"] == [DENY]


async def test_startup_removes_raw_key_reference_even_when_already_outside_team_ceiling(setup):
    service, fake, (_, alice, bob) = setup
    service.users = {alice.name: replace(alice, allowed_mcps=("tasks",))}
    fake.teams[alice.name]["object_permission"]["mcp_servers"] = ["tasks"]
    await prune_configured_access(service)
    assert fake.keys[alice.litellm_key]["object_permission"]["mcp_servers"] == ["tasks"]
    assert [path for path, _ in fake.writes] == ["/key/update"]


async def test_startup_removes_deleted_registry_references_and_orphan_tool_filters(setup):
    service, fake, (_, alice, _) = setup
    service.users = {alice.name: replace(alice, allowed_mcps=("tasks", "docs"))}
    for record in (fake.keys[alice.litellm_key], fake.teams[alice.name]):
        record["object_permission"]["mcp_servers"].append("deleted-server")
        record["object_permission"]["mcp_tool_permissions"] = {"deleted-server": ["read"], "orphan": ["read"]}
    await prune_configured_access(service)
    for record in (fake.keys[alice.litellm_key], fake.teams[alice.name]):
        assert set(record["object_permission"]["mcp_servers"]) == {"tasks", "docs"}
        assert record["object_permission"]["mcp_tool_permissions"] == {}


@pytest.mark.parametrize("ambiguous", [False, True])
async def test_startup_resolves_all_aliases_before_any_write(setup, ambiguous):
    service, fake, (_, alice, bob) = setup
    if ambiguous:
        fake.registry.append({"server_id": "another", "alias": "tasks"})
    service.users[bob.name] = replace(bob, allowed_mcps=("tasks" if ambiguous else "missing",))
    with pytest.raises(Problem, match="missing or ambiguous"):
        await prune_configured_access(service)
    assert fake.writes == []


async def test_startup_partial_write_can_complete_on_next_start(setup):
    service, fake, (_, alice, _) = setup
    service.users = {alice.name: replace(alice, allowed_mcps=("tasks",))}
    original = service.upstream.write_key

    async def fail_key(*args):
        raise Problem(502, "Unavailable")

    service.upstream.write_key = fail_key
    with pytest.raises(Problem):
        await prune_configured_access(service)
    assert fake.teams[alice.name]["object_permission"]["mcp_servers"] == ["tasks"]
    assert service.db.operation(alice.name)["status"] == "uncertain"
    service.upstream.write_key = original
    await prune_configured_access(service)
    assert fake.keys[alice.litellm_key]["object_permission"]["mcp_servers"] == ["tasks"]
    assert service.db.operation(alice.name)["status"] == "verified"
