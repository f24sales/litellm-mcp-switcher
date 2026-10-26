"""Remove out-of-configuration access from managed keys before opening listeners."""
import logging

from .litellm import DENY, intersect
from .models import Problem, ProjectInput, Selection


async def prune_configured_access(service):
    inventory = await service.upstream.inventory()
    plans = []
    # Resolve every configured alias before changing any key. Never delete registry entries.
    for target in service.users.values():
        allowed = {}
        for alias in target.allowed_mcps:
            matches = [s["server_id"] for s in inventory if s.get("alias") == alias]
            if len(matches) != 1:
                raise Problem(409, f"Allowed MCP alias is missing or ambiguous for {target.name}")
            allowed[matches[0]] = None
        info, team, ceiling, active = await service.upstream.scope(target, inventory, ignore_missing=True)
        await service.reconcile(target, info, ceiling, active)
        plans.append((target, Selection(servers=allowed), info, team, ceiling, active))

    for target, allowed, info, team, ceiling, active in plans:
        upper = intersect(ceiling, allowed)
        selected = intersect(active, upper)
        desired = {"ceiling": upper.model_dump(), "active": selected.model_dump()}
        reference_names = {value for server in inventory if server["server_id"] in allowed.servers
                           for field in ("server_id", "alias", "server_name") if (value := server.get(field))}

        def outside_configuration(record):
            permission = record.get("object_permission") or {}
            references = set(permission.get("mcp_servers") or []) | set(permission.get("mcp_tool_permissions") or {})
            return bool(references - reference_names - {DENY})

        # An empty list can inherit access in LiteLLM; empty configuration must be explicit deny.
        team_deny = (team.get("object_permission") or {}).get("mcp_servers") == [DENY]
        key_deny = (info.get("object_permission") or {}).get("mcp_servers") == [DENY]
        write_team = upper != ceiling or outside_configuration(team) or (not upper.servers and not team_deny)
        write_key = (selected.model_dump() != info["_switcher_raw_active"] or outside_configuration(info)
                     or (not selected.servers and not key_deny))
        if write_team or write_key:
            with service.db.lease(target.name, "ceiling", desired,
                                  timeout=service.settings.litellm_request_timeout_seconds * 12 + 60) as op:
                fresh_info, fresh_team, fresh_upper, fresh_active = await service.upstream.scope(
                    target, inventory, ignore_missing=True)
                if (fresh_info.get("team_id") != info.get("team_id")
                        or fresh_info.get("object_permission") != info.get("object_permission")
                        or fresh_team.get("object_permission") != team.get("object_permission")
                        or (fresh_upper, fresh_active) != (ceiling, active)):
                    raise Problem(409, f"MCP access changed during startup for {target.name}")
                if write_team:
                    await service.upstream.write_team(team, upper)
                if write_key:
                    await service.upstream.write_key(target, info, selected)
                verified, _, actual_upper, _ = await service.upstream.scope(target, inventory)
                if {"ceiling": actual_upper.model_dump(), "active": verified["_switcher_raw_active"]} != desired:
                    raise Problem(502, f"LiteLLM did not confirm startup cleanup for {target.name}")
                service.db.finish(target.name, op, "verified", "Configured MCP access verified at startup")
            logging.warning("Startup MCP cleanup applied to bearer alias %s", target.name)

        # Presets belong to individual managed keys; retain their names and remaining tool filters.
        for project in service.db.list_projects(target.name):
            before = Selection(**project["selection"])
            after = intersect(before, allowed)
            if before != after:
                service.db.save_project(target.name, ProjectInput(name=project["name"], selection=after,
                                        version=project["version"]), project["id"])
