"""Administrative lifecycle for managed aliases, using the private rotation journal."""
import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import logging
import re
import secrets
import uuid

from .config import Identity
from .litellm import merge_permission
from .models import Problem, Selection
from .permissions import edit, read
from .rotation import digest


def alias_revision(target):
    return digest([target.owner, target.name, target.litellm_key, target.bearer])


class AliasManager:
    def __init__(self, service):
        self.service = service
        self.store = service.rotation.store
        self.lock = asyncio.Lock()

    def validate_name(self, name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise Problem(422, "Use a valid alias of up to 64 characters")
        if any(identity.name == name for identity in self.service.identities):
            raise Problem(409, "This alias is already in use")
        if name in read(self.service.settings.permissions_file):
            raise Problem(409, "A saved permissions entry already uses this alias")

    async def change(self, actor, kind, name, new=None, revision=None):
        if not actor.admin:
            raise Problem(403, "Administrative access required")
        async with self.lock, AsyncExitStack() as stack:
            if name in self.service.key_locks:
                await stack.enter_async_context(self.service.key_locks[name])
            with self.store.locked():
                data = self.store.read()
                changes = data.setdefault("alias_changes", {})
                op = changes.get(name)
                if op:
                    if (op["kind"], op["name"], op.get("new")) != (kind, name, new):
                        raise Problem(409, f"Complete the pending {op['kind']} for {op['name']} first")
                else:
                    if any(item.get("new") in {name, new} - {None} or item["name"] == new
                           for item in changes.values()):
                        raise Problem(409, "This name belongs to a pending alias change")
                    if kind in ("create", "rename"):
                        self.validate_name(new if kind == "rename" else name)
                    if kind == "create":
                        op = {"source": "+" + uuid.uuid4().hex, "team": str(uuid.uuid4())}
                    else:
                        target = self.service.target(actor, name)
                        if alias_revision(target) != revision:
                            raise Problem(409, "Alias or credentials changed. Reload the alias before continuing")
                        if any(item["alias"] == name and item["status"] in ("pending", "uncertain", "retryable")
                               for item in data["operations"].values()):
                            raise Problem(409, "Finish the pending virtual key rotation first")
                        if self.service.db.operation(target.owner)["status"] in ("pending", "uncertain"):
                            raise Problem(409, "Refresh and reconcile the pending access change first")
                        op = {"source": target.owner, "key": target.litellm_key}
                        if kind == "login":
                            op["login"] = secrets.token_urlsafe(32)
                        op["allowed"] = list(target.allowed_mcps)
                        if kind == "rename":
                            op["new"] = new
                    op.update(kind=kind, name=name)
                    changes[name] = op
                    self.store.write(data)
                await self.finish(data, op)
        return {"target_key": new or name}

    async def recover(self):
        if not self.store.read().get("alias_changes"):
            return
        with self.store.locked():
            data = self.store.read()
            for op in list(data.get("alias_changes", {}).values()):
                try:
                    await self.finish(data, op)
                except Problem:
                    logging.warning("Pending alias %s for %s requires an administrative retry", op["kind"], op["name"])

    async def provision(self, data, op):
        upstream = self.service.upstream
        key, team, name = op.get("key"), op["team"], op["name"]
        permission = merge_permission({}, Selection())
        marker = {"mcp_switcher_owner": op["source"]}
        try:
            await upstream.request("POST", "/team/new", json={"team_id": team, "team_alias": "mcp-switcher-" + name,
                                   "models": ["no-default-models"], "metadata": marker,
                                   "object_permission": permission})
        except Problem:
            # A previous attempt may have created this specific team before losing its reply.
            pass
        record = await upstream.request("GET", "/team/info", params={"team_id": team})
        info = record.get("team_info") or {}
        if (info.get("team_id") != team or (info.get("metadata") or {}).get("mcp_switcher_owner") != op["source"]
                or (info.get("object_permission") or {}).get("mcp_servers") != permission["mcp_servers"]):
            raise Problem(409, "Cannot verify ownership and access of the new dedicated team")
        parameters = {"team_id": team, "key_alias": "mcp-switcher-" + name,
                      "models": ["no-default-models"], "allowed_routes": ["mcp_inference_routes", "/key/info"],
                      "metadata": marker, "object_permission": permission}
        info = await upstream.inspect_key(key) if key else None
        if info is None:
            keys = record.get("keys")
            if keys != []:
                # A lost generate response leaves only a hash in LiteLLM. Recover exclusively
                # our unpublished key, identified by the operation's unique ownership marker.
                orphan = keys[0] if isinstance(keys, list) and len(keys) == 1 else None
                if (key or not isinstance(orphan, dict) or orphan.get("team_id") != team
                        or orphan.get("key_alias") != parameters["key_alias"]
                        or (orphan.get("metadata") or {}).get("mcp_switcher_owner") != op["source"]
                        or not re.fullmatch(r"[0-9a-f]{64}", orphan.get("token") or "")):
                    raise Problem(409, "The dedicated team already contains another key")
                await upstream.delete_key(orphan["token"])
                checked = await upstream.request("GET", "/team/info", params={"team_id": team})
                if checked.get("keys") != []:
                    raise Problem(409, "The previous key is still present")
            if key:
                # Resume a create journaled by a release that preselected the key.
                await upstream.create_replacement_key(key, parameters)
            else:
                key = await upstream.generate_virtual_key(parameters)
                op["key"] = key
                self.store.write(data)
            info = await upstream.inspect_key(key)
        if not info or info.get("team_id") != team or info.get("key_alias") != "mcp-switcher-" + name:
            raise Problem(502, "New virtual key could not be verified")

    async def confirm_upstream(self, data, op):
        upstream = self.service.upstream
        kind, key = op["kind"], op.get("key")
        if kind == "create":
            await self.provision(data, op)
        elif kind == "rename":
            info = await upstream.inspect_key(key)
            if info is None:
                raise Problem(409, "Virtual key is unavailable")
            if "upstream_name" not in op:
                op["upstream_name"] = ("mcp-switcher-" if (info.get("key_alias") or "").startswith("mcp-switcher-")
                                       else "") + op["new"]
            if info.get("key_alias") != op["upstream_name"]:
                await upstream.restore_key_alias(key, op["upstream_name"])
                info = await upstream.inspect_key(key)
                if not info or info.get("key_alias") != op["upstream_name"]:
                    raise Problem(502, "Virtual key rename could not be verified")
        elif kind == "delete":
            if await upstream.managed_key_exists(key):
                await upstream.delete_key(key)
                if await upstream.managed_key_exists(key):
                    raise Problem(502, "Virtual key revocation could not be verified")

    async def finish(self, data, op):
        kind, name, source = op["kind"], op["name"], op["source"]
        new = op.get("new")
        if not op.get("confirmed"):
            await self.confirm_upstream(data, op)
            op["confirmed"] = True
            self.store.write(data)
        catalog = data.setdefault("catalog", {})
        record = catalog.setdefault(source, {"name": name})
        if kind != "login":
            with edit(self.service.settings.permissions_file) as entries:
                if kind == "create":
                    entries.setdefault(name, [])
                elif kind == "rename" and name in entries:
                    entries[new] = entries.pop(name)
                elif kind == "delete":
                    entries.pop(name, None)
        users = self.service.users
        old = users.get(name)
        if kind == "create":
            record.update(seed=op["key"], login=op.get("login", ""))
            target = Identity(name, record["login"], record["seed"], source=source)
            self.service.db.initialize_owners([source])
        elif kind == "delete":
            if source.startswith("+"):
                del catalog[source]
            else:
                catalog[source] = {"name": None}
            self.service.db.remove_owner(source)
            target = None
        elif kind == "rename":
            record["name"] = new
            target = replace(old, name=new, source=source)
        else:
            record["login"] = op["login"]
            target = replace(old, bearer=op["login"])
        if kind in ("delete", "rename"):
            rotated = data["keys"].pop(name, None)
            if rotated and new:
                data["keys"][new] = rotated
            for oid, item in list(data["operations"].items()):
                if item["alias"] == name:
                    if new:
                        item["alias"] = new
                    else:
                        del data["operations"][oid]
        del data["alias_changes"][name]
        self.store.write(data)
        # No await between the durable commit and replacing both live listener views.
        identities = []
        for identity in self.service.identities:
            if identity.name == name and old:
                continue
            if identity.orchestrator and name in identity.key_names and kind in ("rename", "delete"):
                names = tuple(new if key == name else key for key in identity.key_names if new or key != name)
                identity = replace(identity, key_names=names)
            identities.append(identity)
        if old:
            del users[name]
        if target:
            users[target.name] = target
            identities.append(target)
            self.service.key_locks.setdefault(target.name, asyncio.Lock())
        self.service.identities[:] = identities
