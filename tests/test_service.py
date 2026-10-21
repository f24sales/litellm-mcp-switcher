import copy
import time
from dataclasses import replace

import pytest
from sqlalchemy import update

from mcp_switcher.models import ApplyInput, Problem, ProjectInput, Selection
from mcp_switcher.litellm import DENY


def selection(**servers):
    return Selection(servers=servers)


async def test_always_on_survives_clear_tool_filters_projects_and_admin_ceiling(setup):
    service, fake, (admin, original, bob) = setup
    alice = replace(original, always_on_mcps=("tasks",))
    service.users[alice.name] = alice
    before = copy.deepcopy(fake.keys[alice.litellm_key])
    for selected in (selection(), selection(tasks=["read"]), selection(tasks=[])):
        state = await service.state(alice)
        assert state["always_on_mcps"] == ["tasks"]
        result = await service.apply(alice, ApplyInput(selection=selected, revision=state["revision"]))
        assert result["active"]["servers"] == {"tasks": None}
        assert fake.keys[alice.litellm_key]["object_permission"]["mcp_tool_permissions"] == {}
    project = await service.save_project(alice, ProjectInput(name="No optional servers", selection=selection()))
    assert project["selection"] == {"servers": {"tasks": None}}
    state = await service.state(admin, alice.name)
    result = await service.apply(admin, ApplyInput(selection=selection(), revision=state["revision"]),
                                 alice.name, ceiling_change=True)
    assert result["ceiling"]["servers"] == {"tasks": None}
    assert result["active"]["servers"] == {"tasks": None}
    bob_state = await service.state(bob)
    result = await service.apply(bob, ApplyInput(selection=selection(), revision=bob_state["revision"]))
    assert result["active"]["servers"] == {}
    assert fake.keys[bob.litellm_key]["object_permission"]["mcp_servers"] == [DENY]
    for field in ("models", "budget_id", "team_id"):
        assert fake.keys[alice.litellm_key][field] == before[field]


async def test_always_on_does_not_bypass_team_tool_restrictions(setup):
    service, fake, (_, original, _) = setup
    alice = replace(original, always_on_mcps=("tasks",))
    fake.teams[alice.name]["object_permission"]["mcp_tool_permissions"] = {"tasks": ["read"]}
    state = await service.state(alice)
    with pytest.raises(Problem, match="unrestricted tools"):
        await service.apply(alice, ApplyInput(selection=selection(), revision=state["revision"]))
    assert fake.writes == []


async def test_missing_ambiguous_alias_and_changed_configuration_fail_closed(setup):
    service, fake, (_, alice, _) = setup
    state = await service.state(alice)
    with pytest.raises(Problem, match="changed"):
        await service.apply(replace(alice, always_on_mcps=("tasks",)),
                            ApplyInput(selection=selection(), revision=state["revision"]))
    with pytest.raises(Problem, match="missing or ambiguous"):
        await service.state(replace(alice, always_on_mcps=("absent",)))
    fake.registry.append({"server_id": "different", "alias": "tasks", "server_name": "Duplicate"})
    with pytest.raises(Problem, match="missing or ambiguous"):
        await service.state(replace(alice, always_on_mcps=("tasks",)))
    assert fake.writes == []


async def test_projects_owned_versioned_and_persistent(setup):
    service, fake, (_, alice, bob) = setup
    before = copy.deepcopy(fake.keys)
    data = ProjectInput(name="Work", selection=selection(tasks=None))
    project = await service.save_project(alice, data)
    assert project["version"] == 1
    assert fake.keys == before  # Saving never activates access.
    assert service.db.list_projects(bob.name) == []
    with pytest.raises(Problem):
        service.delete_project(bob, project["id"], 1)
    with pytest.raises(Problem):
        await service.save_project(bob, data, project["id"])
    saved = await service.save_project(alice, data.model_copy(update={"name": "Renamed", "version": 1}), project["id"])
    assert saved["version"] == 2
    with pytest.raises(Problem):
        await service.save_project(alice, data.model_copy(update={"version": 1}), project["id"])
    service.db.migrate()
    assert service.db.list_projects(alice.name)[0]["name"] == "Renamed"


async def test_clear_all_and_preserve_unrelated_key_fields(setup):
    service, fake, (_, alice, _) = setup
    before = copy.deepcopy(fake.keys[alice.litellm_key])
    state = await service.state(alice)
    state = await service.apply(alice, ApplyInput(selection=selection(), revision=state["revision"]))
    assert state["active"]["servers"] == {}
    after = fake.keys[alice.litellm_key]
    assert after["object_permission"]["mcp_servers"] == [DENY]
    assert after["object_permission"]["vector_stores"] == ["keep-me"]
    for key in ("models", "budget_id", "team_id"):
        assert after[key] == before[key]
    assert set(state["ceiling"]["servers"]) == {"tasks", "docs"}


async def test_revoke_caps_active_and_old_project(setup):
    service, fake, (admin, alice, _) = setup
    project = await service.save_project(alice, ProjectInput(name="Both", selection=selection(tasks=None, docs=None)))
    state = await service.state(admin, "alice")
    await service.apply(admin, ApplyInput(selection=selection(tasks=["read"]), revision=state["revision"]),
                        "alice", ceiling_change=True)
    state = await service.state(alice)
    assert state["active"]["servers"] == {"tasks": ["read"]}
    assert [s["server_id"] for s in state["servers"]] == ["tasks"]
    with pytest.raises(Problem):
        await service.apply(alice, ApplyInput(selection=project["selection"], revision=state["revision"]))
    with pytest.raises(Problem):
        await service.apply(alice, ApplyInput(selection=selection(tasks=["write"]), revision=state["revision"]))
    with pytest.raises(Problem):
        await service.apply(alice, ApplyInput(selection=selection(), revision=state["revision"]),
                            ceiling_change=True)


async def test_admin_empty_ceiling_is_explicit_deny(setup):
    service, fake, (admin, alice, _) = setup
    state = await service.state(admin, "alice")
    await service.apply(admin, ApplyInput(selection=selection(), revision=state["revision"]),
                        "alice", ceiling_change=True)
    assert fake.teams["alice"]["object_permission"]["mcp_servers"] == [DENY]
    assert (await service.state(alice))["active"] == {"servers": {}}


async def test_conflict_and_cross_request_lease(setup):
    service, fake, (_, alice, _) = setup
    state = await service.state(alice)
    fake.keys[alice.litellm_key]["object_permission"]["mcp_servers"] = ["tasks"]
    with pytest.raises(Problem, match="changed"):
        await service.apply(alice, ApplyInput(selection=selection(), revision=state["revision"]))
    state = await service.state(alice)
    with service.db.lease("alice", "active", selection().model_dump()) as operation_id:
        with pytest.raises(Problem, match="reconciliation"):
            await service.apply(alice, ApplyInput(selection=selection(), revision=state["revision"]))
        service.db.finish("alice", operation_id, "conflict", "Test completed")


async def test_timeout_reconciles_actual_result(setup):
    service, fake, (_, alice, _) = setup
    state = await service.state(alice)
    fake.uncertain_write = True
    with pytest.raises(Problem):
        await service.apply(alice, ApplyInput(selection=selection(tasks=None), revision=state["revision"]))
    assert service.db.operation("alice")["status"] == "uncertain"
    state = await service.state(alice)
    assert state["operation"]["status"] == "verified"
    assert state["active"]["servers"] == {"tasks": None}
    assert len(fake.writes) == 1


async def test_failed_write_never_reports_success(setup):
    service, fake, (_, alice, _) = setup
    state = await service.state(alice)
    fake.fail_write = True
    with pytest.raises(Problem):
        await service.apply(alice, ApplyInput(selection=selection(), revision=state["revision"]))
    state = await service.state(alice)
    assert state["operation"]["status"] == "conflict"
    assert len(state["active"]["servers"]) == 2


async def test_expired_operation_after_process_crash_is_reconciled(setup):
    service, fake, (_, alice, _) = setup
    with service.db.engine.begin() as conn:
        conn.execute(update(service.db.operations).where(service.db.operations.c.owner == "alice").values(
            id="crash", status="pending", kind="active", expires=time.time()-1,
            target=selection(tasks=None).model_dump()))
    assert (await service.state(alice))["operation"]["status"] == "conflict"


async def test_unavailable_discovery_preserves_selection(setup):
    service, fake, (_, alice, bob) = setup
    fake.fail_tools = True
    assert (await service.tools(alice, "tasks"))["status"] == "error"
    assert len((await service.state(alice))["active"]["servers"]) == 2
    with pytest.raises(Problem):
        await service.tools(alice, "unregistered")
    with pytest.raises(Problem):
        await service.state(alice, bob.name)
    with pytest.raises(Problem):
        await service.apply(alice, ApplyInput(selection=selection(tasks=["nonexistent"]),
                            revision=(await service.state(alice))["revision"]))


async def test_valid_tool_subset_and_zero_tools(setup):
    service, fake, (_, alice, _) = setup
    state = await service.state(alice)
    result = await service.apply(alice, ApplyInput(selection=selection(tasks=["read"]), revision=state["revision"]))
    assert result["active"]["servers"] == {"tasks": ["read"]}
    assert selection(tasks=[]).servers == {}


async def test_unsafe_group_configuration_fails_closed(setup):
    service, fake, (_, alice, _) = setup
    fake.keys[alice.litellm_key]["object_permission"]["mcp_access_groups"] = ["additive"]
    with pytest.raises(Problem, match="additive"):
        await service.state(alice)


async def test_all_permitted_tools_resolves_to_team_subset(setup):
    service, fake, (admin, alice, _) = setup
    state = await service.state(admin, "alice")
    await service.apply(admin, ApplyInput(selection=selection(tasks=["read"]), revision=state["revision"]),
                        "alice", ceiling_change=True)
    state = await service.state(alice)
    result = await service.apply(alice, ApplyInput(selection=selection(tasks=None), revision=state["revision"]))
    assert result["active"]["servers"] == {"tasks": ["read"]}


async def test_partial_admin_update_is_not_reported_verified(setup):
    service, fake, (admin, alice, _) = setup
    state = await service.state(admin, "alice")
    original = service.upstream.write_key

    async def fail_key(*args):
        raise Problem(502, "Key cleanup failed")

    service.upstream.write_key = fail_key
    with pytest.raises(Problem):
        await service.apply(admin, ApplyInput(selection=selection(tasks=None), revision=state["revision"]),
                            "alice", ceiling_change=True)
    service.upstream.write_key = original
    state = await service.state(alice)
    assert state["operation"]["status"] == "conflict"
    assert state["active"]["servers"] == {"tasks": None}
