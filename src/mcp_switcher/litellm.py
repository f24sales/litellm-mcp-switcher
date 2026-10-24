"""Small LiteLLM management client; never returns upstream secrets."""
import json

import httpx

from .models import Problem, Selection

DENY = "no-mcp-servers"
MCP_FIELDS = {"mcp_servers", "mcp_access_groups", "mcp_tool_permissions", "mcp_toolsets"}


def permission_selection(permission: dict, inventory: list[dict]) -> Selection:
    """Resolve stored ids/names/aliases; never interpret an empty scope as all."""
    if permission.get("mcp_access_groups") or permission.get("mcp_toolsets"):
        raise Problem(409, "This key or team uses additive MCP groups/toolsets; use a dedicated explicit scope")
    names = {name: s["server_id"] for s in inventory for name in
             (s["server_id"], s.get("alias"), s.get("server_name")) if name}
    ids = permission.get("mcp_servers") or []
    if DENY in ids:
        return Selection()
    if any(x not in names for x in ids):
        raise Problem(409, "The MCP scope contains an unresolved server; reconcile the LiteLLM registry")
    tools = permission.get("mcp_tool_permissions") or {}
    normalized_tools = {names[k]: v for k, v in tools.items() if k in names}
    return Selection(servers={names[x]: normalized_tools.get(names[x]) for x in ids})


def merge_permission(existing: dict, selection: Selection) -> dict:
    # Preserve independent permissions and replace the entire MCP selection together.
    result = {k: v for k, v in existing.items() if k != "object_permission_id"}
    result.update(mcp_servers=list(selection.servers) or [DENY], mcp_access_groups=[], mcp_toolsets=[],
                  mcp_tool_permissions={s: t for s, t in selection.servers.items() if t is not None})
    return result


def intersect(selection: Selection, ceiling: Selection) -> Selection:
    servers = {}
    for sid, tools in selection.servers.items():
        if sid not in ceiling.servers:
            continue
        upper = ceiling.servers[sid]
        if upper is None:
            servers[sid] = tools
        elif tools is None:
            servers[sid] = upper
        else:
            servers[sid] = sorted(set(tools) & set(upper))
    return Selection(servers=servers)


class LiteLLM:
    def __init__(self, settings, transport=None):
        self.admin = settings.litellm_admin_virtual_key.get_secret_value()
        self.client = httpx.AsyncClient(base_url=settings.litellm_api_base_url.rstrip("/"),
            timeout=settings.litellm_request_timeout_seconds, transport=transport, follow_redirects=False)

    async def request(self, method, path, *, token=None, **kwargs):
        try:
            response = await self.client.request(method, path,
                headers={"Authorization": "Bearer " + (token or self.admin)}, **kwargs)
        except httpx.HTTPError:
            raise Problem(502, "LiteLLM connection failed; the result of a write may be uncertain") from None
        if response.status_code >= 400:
            raise Problem(502, f"LiteLLM rejected the operation (HTTP {response.status_code})")
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            raise Problem(502, "LiteLLM returned an invalid response") from None

    async def inventory(self):
        data = await self.request("GET", "/v1/mcp/server")
        if not isinstance(data, list):
            raise Problem(502, "LiteLLM MCP registry is unavailable")
        return [{k: s.get(k) for k in ("server_id", "server_name", "alias", "description")}
                for s in data]

    async def scope(self, identity, inventory):
        key = await self.request("GET", "/key/info", token=identity.litellm_key)
        info = key["info"]
        if not info.get("team_id"):
            raise Problem(409, "A dedicated LiteLLM team is required for each managed key")
        team = await self.request("GET", "/team/info", params={"team_id": info["team_id"]})
        if len(team.get("keys", [])) != 1:
            raise Problem(409, "The dedicated MCP team must contain exactly one key")
        key_permission = info.get("object_permission") or {}
        team_permission = team["team_info"].get("object_permission") or {}
        ceiling = permission_selection(team_permission, inventory)
        active = permission_selection(key_permission, inventory)
        # Empty key scope would inherit the team: show its real effective state.
        if not key_permission.get("mcp_servers") and not key_permission.get("mcp_tool_permissions"):
            active = ceiling
        info["_switcher_raw_active"] = active.model_dump()
        active = intersect(active, ceiling)
        return info, team["team_info"], ceiling, active

    async def write_key(self, identity, info, selection):
        await self.request("POST", "/key/update", json={"key": identity.litellm_key,
            "object_permission": merge_permission(info.get("object_permission") or {}, selection)})

    async def write_team(self, team, selection):
        await self.request("POST", "/team/update", json={"team_id": team["team_id"],
            "object_permission": merge_permission(team.get("object_permission") or {}, selection)})

    async def refresh_tools(self, identity):
        data = await self.request("POST", "/mcp/notify-tools-changed", token=identity.litellm_key, json={})
        if (data.get("notification") != "notifications/tools/list_changed"
                or data.get("permissions_changed") is not False):
            raise Problem(502, "LiteLLM did not confirm the tool-list change notification")
        return {key: data.get(key) for key in ("notification", "notified_connections",
                                             "permissions_changed", "client_refresh_required")}

    async def tools(self, server_id):
        data = await self.request("GET", "/mcp-rest/tools/list", params={"server_id": server_id})
        if data.get("status") in {"error", "failed"} or data.get("error"):
            raise Problem(502, "Upstream tool discovery failed")
        tools = data.get("tools")
        if not isinstance(tools, list):
            raise Problem(502, "Upstream tool discovery returned an invalid catalog")
        return [{"name": t["name"], "description": (t.get("description") or "")[:1500]} for t in tools]

    async def close(self):
        await self.client.aclose()
