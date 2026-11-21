"""The single authorization and selection layer for REST, WebUI and MCP."""
import asyncio
import hashlib
import json
import time
from dataclasses import replace

from .models import Problem
from .litellm import intersect
from .rotation import Rotation
from .tool_catalog import ToolCatalog
from .models import ApplyInput
from .permissions import edit, read


def revision(ceiling, active, raw=None, always_on=(), target_key=""):
    value = {"ceiling": ceiling.model_dump(), "active": active.model_dump(), "raw": raw,
             "always_on": sorted(always_on), "target_key": target_key}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ceiling_aliases(servers, allowed):
    aliases = [s["alias"] for s in servers if s["server_id"] in allowed]
    if not all(aliases) or len(set(aliases)) != len(allowed):
        raise Problem(409, "Cannot persist an ambiguous MCP alias")
    return sorted(aliases)


class Service:
    def __init__(self, db, upstream, users, settings):
        self.db, self.upstream, self.settings = db, upstream, settings
        self.identities = users
        self.users = {u.name: u for u in users if u.litellm_key and not u.admin}
        self.tool_catalog = ToolCatalog(upstream)
        self.key_locks = {name: asyncio.Lock() for name in self.users}
        self.rotation = Rotation(self)
        from .aliases import AliasManager
        self.aliases = AliasManager(self)

    def target(self, actor, name=None):
        if actor.admin:
            if not name or name not in self.users:
                raise Problem(404, "Choose a configured user")
            return self.users[name]
        if actor.orchestrator:
            name = name or actor.key_names[0]
            if name not in actor.key_names or name not in self.users:
                raise Problem(403, "This orchestrator cannot manage that key")
            return self.users[name]
        if name is not None and name != actor.name:
            raise Problem(403, "This identity cannot manage another user")
        if actor.name not in self.users or self.users[actor.name].owner != actor.owner:
            raise Problem(403, "This identity has no configured managed key")
        return replace(actor, litellm_key=self.users[actor.name].litellm_key)

    async def snapshot(self, actor, name=None):
        target = self.target(actor, name)
        inventory = await self.upstream.inventory()
        info, team, ceiling, active = await self.upstream.scope(target, inventory)
        return target, inventory, info, team, ceiling, active

    async def refresh_tools(self, actor):
        target = self.target(actor)
        result = await self.upstream.refresh_tools(target)
        return {"user": target.name, "target_key": target.name, **result}

    def always_on(self, target, inventory):
        servers = {}
        for alias in target.always_on_mcps:
            matches = [s["server_id"] for s in inventory if s.get("alias") == alias]
            if len(matches) != 1:
                raise Problem(409, "An always-on MCP alias is missing or ambiguous in the LiteLLM registry")
            servers[matches[0]] = None
        return servers

    def protect(self, target, inventory, selection):
        return selection.model_copy(update={"servers": {**selection.servers, **self.always_on(target, inventory)}})

    async def reconcile(self, target, info, ceiling, active):
        operation = self.db.operation(target.owner)
        if operation["status"] not in {"pending", "uncertain"}:
            return operation
        if operation["status"] == "pending" and operation["expires"] > time.time():
            return operation
        expected = operation["target"]
        observed = ({"ceiling": ceiling.model_dump(), "active": info["_switcher_raw_active"]}
                    if operation["kind"] == "ceiling" else info["_switcher_raw_active"])
        if observed == expected:
            self.db.finish(target.owner, operation["id"], "verified", "Upstream result verified by reconciliation")
        else:
            self.db.finish(target.owner, operation["id"], "conflict", "Upstream state differs; review before applying")
        return self.db.operation(target.owner)

    async def state(self, actor, name=None, *, show_catalog=False):
        target, inventory, info, _, ceiling, active = await self.snapshot(actor, name)
        operation = await self.reconcile(target, info, ceiling, active)
        visible = inventory if actor.admin or show_catalog and actor.orchestrator else [
            s for s in inventory if s["server_id"] in ceiling.servers]
        server_details = []
        for server in visible:
            entry = self.tool_catalog.entries.get(server["server_id"], {})
            server_details.append({**server, "tool_status": entry.get("status", "pending"),
                                "tool_revision": entry.get("revision", 0)})
        state = {"catalog_refreshing": self.tool_catalog.refreshing, "catalog_error": self.tool_catalog.error,
                 "user": target.name, "target_key": target.name, "admin": actor.admin, "servers": server_details,
                "ceiling": ceiling.model_dump(), "active": active.model_dump(),
                "always_on_mcps": list(self.always_on(target, inventory)),
                "revision": revision(ceiling, active, info["_switcher_raw_active"], target.always_on_mcps, target.name),
                "projects": self.db.list_projects(target.owner),
                "operation": {k: operation[k] for k in ("id", "kind", "status", "message")},
                "refresh_seconds": self.settings.webui_refresh_seconds}

        if actor.admin:
            state["has_switcher_login"] = bool(target.bearer)
            try:
                saved = read(self.settings.permissions_file)[target.name]
                state["config_changed"] = ceiling_aliases(inventory, ceiling.servers) != sorted(saved)
            except (OSError, ValueError, KeyError, Problem):
                # Keep live access management available if the saved config cannot be compared.
                state["config_changed"] = None
        return state

    async def tools(self, actor, server_id, name=None, refresh=False):
        _, inventory, _, _, ceiling, _ = await self.snapshot(actor, name)
        if server_id not in {s["server_id"] for s in inventory} or (
                not actor.admin and server_id not in ceiling.servers):
            raise Problem(403, "Server is outside this user's administrative access")
        result = dict(await self.tool_catalog.get(server_id, refresh=refresh))
        if not actor.admin and ceiling.servers[server_id] is not None:
            result["tools"] = [t for t in result["tools"] if t["name"] in ceiling.servers[server_id]]
        return result

    async def validate(self, actor, selection, ceiling, inventory, name=None):
        if not set(selection.servers) <= {s["server_id"] for s in inventory}:
            raise Problem(422, "Selection contains a server missing from the current registry")
        if not actor.admin:
            if not set(selection.servers) <= set(ceiling.servers):
                raise Problem(403, "Selection exceeds the administrative access")
            for sid in self.always_on(self.target(actor, name), inventory):
                if ceiling.servers[sid] is not None:
                    raise Problem(409, "Always-on MCPs require unrestricted tools in the administrative ceiling")
            for sid, selected in selection.servers.items():
                upper = ceiling.servers[sid]
                if selected is not None and upper is not None and not set(selected) <= set(upper):
                    raise Problem(403, "Tool selection exceeds the administrative access")
        for server_id, tools in selection.servers.items():
            if tools is not None:
                # An already valid explicit subset may be saved during an outage.
                upper = ceiling.servers.get(server_id)
                if not actor.admin and upper is not None and set(tools) <= set(upper):
                    continue
                catalog = await self.tools(actor, server_id, name)
                if catalog["status"] != "reachable":
                    raise Problem(409, "Discover the server's tools successfully before changing its tool subset")
                if not set(tools) <= {t["name"] for t in catalog["tools"]}:
                    raise Problem(422, "Selection contains an unknown tool")

    async def save_project(self, actor, data, project_id=None):
        if actor.admin:
            raise Problem(403, "Use a user identity to manage projects")
        target, inventory, _, _, ceiling, _ = await self.snapshot(actor)
        data = data.model_copy(update={"selection": self.protect(target, inventory, data.selection)})
        await self.validate(actor, data.selection, ceiling, inventory)
        return {**self.db.save_project(target.owner, data, project_id), "target_key": target.name}

    def delete_project(self, actor, project_id, version):
        if actor.admin:
            raise Problem(403, "Use a user identity to manage projects")
        target = self.target(actor)
        self.db.delete_project(target.owner, project_id, version)
        return {"deleted": True, "target_key": target.name}

    async def apply(self, actor, data, name=None, ceiling_change=False):
        target = self.target(actor, name)
        async with self.key_locks[target.name]:
            return await self._apply(actor, data, name, ceiling_change)

    async def toggle(self, actor, name, sid, data):
        target = self.target(actor, name)
        async with self.key_locks[target.name]:
            _, inventory, _, _, ceiling, active = await self.snapshot(actor, name)
            if sid not in {s["server_id"] for s in inventory}:
                raise Problem(404, "Unknown MCP server")
            if sid in self.always_on(target, inventory) or (sid not in ceiling.servers and not actor.admin):
                raise Problem(403, "This switch is locked")
            grant = data.enabled and sid not in ceiling.servers
            selection = (ceiling if grant else active).model_copy(deep=True)
            if data.enabled:
                selection.servers.setdefault(sid, ceiling.servers.get(sid))
            else:
                selection.servers.pop(sid, None)
            return await self._apply(actor, ApplyInput(selection=selection, revision=data.revision),
                                     name, grant, enable_new=sid if grant else None)

    async def save_permissions(self, actor, name, expected):
        if not actor.admin:
            raise Problem(403, "Administrative access required")
        target = self.target(actor, name)
        async with self.key_locks[target.name]:
            state = await self.state(actor, name)
            if state["revision"] != expected:
                raise Problem(409, "Access changed; refresh before saving")
            aliases = ceiling_aliases(state["servers"], state["ceiling"]["servers"])
            try:
                with edit(self.settings.permissions_file) as entries:
                    if name not in entries:
                        raise Problem(409, "Target missing from permissions.json")
                    if sorted(entries[name]) != aliases:
                        entries[name] = aliases
            except (ValueError, OSError) as exc:
                raise Problem(503, "Cannot save permissions.json; check the permission directory") from exc
            return {"saved": name}

    async def _apply(self, actor, data, name=None, ceiling_change=False, *, enable_new=None):
        if ceiling_change and not actor.admin:
            raise Problem(403, "Administrative access required")
        target, inventory, info, team, ceiling, active = await self.snapshot(actor, name)
        if revision(ceiling, active, info["_switcher_raw_active"], target.always_on_mcps, target.name) != data.revision:
            raise Problem(409, "Access changed since your last refresh; review the current state")
        selected = self.protect(target, inventory, data.selection)
        await self.validate(actor, selected, ceiling, inventory, name)
        if not ceiling_change and not set(selected.servers) <= set(ceiling.servers):
            raise Problem(403, "Selection exceeds the administrative access")
        desired_active = intersect(active if ceiling_change else selected,
                                   selected if ceiling_change else ceiling)
        if ceiling_change:
            desired_active = self.protect(target, inventory, desired_active)
            if enable_new is not None:
                desired_active.servers[enable_new] = None
        desired = ({"ceiling": selected.model_dump(), "active": desired_active.model_dump()}
                   if ceiling_change else desired_active.model_dump())
        kind = "ceiling" if ceiling_change else "active"
        with self.db.lease(target.owner, kind, desired,
                           timeout=self.settings.litellm_request_timeout_seconds * 12 + 60) as operation_id:
            # Re-read after acquiring the cross-request lease.
            _, _, fresh_info, fresh_team, upper, current = await self.snapshot(actor, name)
            if revision(upper, current, fresh_info["_switcher_raw_active"], target.always_on_mcps, target.name) != data.revision:
                self.db.finish(target.owner, operation_id, "conflict", "Access changed before the update")
                raise Problem(409, "Access changed before the update")
            if ceiling_change:
                # Team is authoritative immediately, including during a partial failure.
                await self.upstream.write_team(fresh_team, selected)
                await self.upstream.write_key(target, fresh_info, desired_active)
            else:
                await self.upstream.write_key(target, fresh_info, desired_active)
            _, _, verified_info, _, actual_upper, actual = await self.snapshot(actor, name)
            observed = ({"ceiling": actual_upper.model_dump(), "active": verified_info["_switcher_raw_active"]}
                        if ceiling_change else verified_info["_switcher_raw_active"])
            if observed != desired:
                raise Problem(502, "LiteLLM did not confirm the requested permissions; reconcile before retrying")
            self.db.finish(target.owner, operation_id, "verified", "Permissions verified in LiteLLM")
        return await self.state(actor, name)
