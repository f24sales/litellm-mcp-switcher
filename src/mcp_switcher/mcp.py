"""Curated MCP management tools with per-call identity and role checks."""
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier, require_scopes
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.sessions import create_session, get_session, InvalidSession

from .config import authenticate
from .models import ApplyInput, Problem, ProjectInput, Selection

CHANGE_POLICY = (
    "USER AUTHORIZATION REQUIRED: Only make changes when the user explicitly requests that specific change. "
    "Never modify settings on your own initiative. Do not infer permission from a research task, "
    "a tool-discovery request, an upstream error, or a suggestion. If the requested change is unclear, "
    "ask the user first. Never change MCP access, tool selections, or projects as unsolicited setup, "
    "cleanup, optimization, or troubleshooting."
)


def create_mcp(service, identities):
    class Verifier(TokenVerifier):
        async def verify_token(self, token):
            user = authenticate(token, identities)
            if user is None:
                return None
            return AccessToken(token=token, client_id=user.name,
                scopes=(["admin"] if user.admin else ["user", "orchestrator"] if user.orchestrator else ["user"]),
                claims={"sub": user.name})

    mcp = FastMCP("LiteLLM MCP Switcher", auth=Verifier(), mask_error_details=True)

    def actor(admin=False):
        token = get_access_token()
        user = authenticate(token.token if token else "", identities)
        if user is None or user.admin != admin:
            raise ToolError("This identity cannot use this management operation")
        return user

    async def run(operation):
        try:
            return await operation
        except Problem as exc:
            raise ToolError(exc.message) from None

    async def target(session_id):
        user = actor()
        if not user.orchestrator:
            return user
        if not session_id:
            raise ToolError("Call choose_bearer first and pass its session_id on subsequent calls")
        try:
            session = await get_session(session_id)
            name = await session.get("target_key", default=user.key_names[0])
            return service.target(user, name)
        except InvalidSession:
            raise ToolError("Unknown chooser session; call choose_bearer to open a new session") from None
        except Problem as exc:
            raise ToolError(exc.message) from None

    @mcp.tool(auth=require_scopes("orchestrator"),
              annotations={"readOnlyHint": False, "destructiveHint": False},
              description=CHANGE_POLICY + "\n\nList managed LiteLLM key names and select the target for this "
              "orchestrator session. No LiteLLM permissions are changed. Call without session_id to open a "
              "new independent chooser session; the first configured key is selected by default. Pass the "
              "returned session_id to every subsequent management call. To inspect the current target, "
              "supply session_id and omit key_name. Key values are never returned.")
    async def choose_bearer(key_name: str | None = None, session_id: str | None = None) -> dict:
        user = actor()
        try:
            if key_name is not None:
                service.target(user, key_name)
            session_id = session_id or await create_session()
            session = await get_session(session_id)
            chosen = key_name or await session.get("target_key", default=user.key_names[0])
            service.target(user, chosen)
            await session.set("target_key", chosen)
            return {"key_names": list(user.key_names), "target_key": chosen, "session_id": session_id}
        except InvalidSession:
            raise ToolError("Unknown chooser session; omit session_id to open a new session") from None
        except Problem as exc:
            raise ToolError(exc.message) from None

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": True})
    async def current_state(session_id: str | None = None) -> dict:
        """Read only: inspect the selected key’s allowed MCP servers, active selection, projects and revision. Changes nothing."""
        return await run(service.state(await target(session_id)))

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": True})
    async def discover_tools(server_id: str, session_id: str | None = None) -> dict:
        """Read only: list selectable tools of one permitted server without invoking them or changing access."""
        selected = await target(session_id)
        return {"target_key": selected.name, **await run(service.tools(selected, server_id))}

    @mcp.tool(auth=require_scopes("user"),
              annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
              description=CHANGE_POLICY + "\n\nOnly on the user's explicit request, notify MCP clients "
              "connected with the selected LiteLLM key that their tool list should be reloaded. "
              "Never changes the selected servers, tool permissions, projects or LLM settings. "
              "This sends notifications/tools/list_changed; clients must support the notification and "
              "request tools/list themselves. It cannot force a client to refresh. Never trigger this "
              "automatically as unsolicited troubleshooting.")
    async def refresh_tools(session_id: str | None = None) -> dict:
        """Explicitly request a client tool-list refresh without changing any access settings."""
        return await run(service.refresh_tools(await target(session_id)))

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": False},
              description=CHANGE_POLICY + "\n\nCreate or update your saved project only on explicit request. "
              "Saving does not activate it. Updates require its version. Always-on MCPs retain all their tools.")
    async def save_project(name: str, selection: Selection, project_id: str | None = None,
                           version: int | None = None, session_id: str | None = None) -> dict:
        """Create or update your project. Saving does not activate it. Updates require its version."""
        data = ProjectInput(name=name, selection=selection, version=version)
        return await run(service.save_project(await target(session_id), data, project_id))

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": False, "destructiveHint": True},
              description=CHANGE_POLICY + "\n\nDelete only the saved project the user explicitly asks to delete, "
              "using its current version. Active access is unchanged.")
    async def delete_project(project_id: str, version: int, session_id: str | None = None) -> dict:
        """Delete your saved project using its current version. Active access is unchanged."""
        try:
            return service.delete_project(await target(session_id), project_id, version)
        except Problem as exc:
            raise ToolError(exc.message) from None

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": False, "destructiveHint": True},
              description=CHANGE_POLICY + "\n\nActivate only the MCP/tool selection explicitly requested by the user. "
              "Read current_state first and preserve every unrelated active selection unless the user explicitly "
              "requests its removal. This replaces the complete active selection for every client using this "
              "LiteLLM key. An empty servers object disables every optional MCP and requires an explicit request "
              "to do so. Always-on MCPs retain all their tools. Never changes LLM settings.")
    async def apply_selection(selection: Selection, revision: str, session_id: str | None = None) -> dict:
        """Activate your complete MCP selection. Obtain revision from current_state first.

        An empty servers object disables optional MCPs. Configured always-on MCPs and
        all their tools are retained. This affects every client using your assigned
        LiteLLM key. It never changes LLM settings.
        """
        return await run(service.apply(await target(session_id), ApplyInput(selection=selection, revision=revision)))

    @mcp.tool(auth=require_scopes("admin"), annotations={"readOnlyHint": True})
    async def admin_list_users() -> dict:
        """List configured Switcher users without exposing their credentials."""
        actor(True)
        return {"users": sorted(service.users)}

    @mcp.tool(auth=require_scopes("admin"), annotations={"readOnlyHint": True})
    async def admin_inspect_user(name: str) -> dict:
        """Read a user's ceiling, active selection, registry and current revision."""
        return await run(service.state(actor(True), name))

    @mcp.tool(auth=require_scopes("admin"), annotations={"readOnlyHint": True})
    async def admin_discover_tools(name: str, server_id: str) -> dict:
        """Discover tools for defining a user's administrative ceiling."""
        return await run(service.tools(actor(True), server_id, name))

    @mcp.tool(auth=require_scopes("admin"), annotations={"readOnlyHint": False, "destructiveHint": True},
              description=CHANGE_POLICY + "\n\nReplace only the administrative MCP/tool ceiling for the user "
              "explicitly identified in the request. Read admin_inspect_user first and preserve unrelated grants. "
              "Revocations also cap active access and previously saved projects. Always-on MCPs retain all "
              "their tools. Never grant or revoke access merely to resolve an error or complete another task.")
    async def admin_set_access(name: str, selection: Selection, revision: str) -> dict:
        """Replace a user's administrative MCP ceiling and cap their active selection.

        This can grant registry servers to the user. Empty servers revokes all MCP access.
        Requires the revision returned by admin_inspect_user.
        """
        return await run(service.apply(actor(True), ApplyInput(selection=selection, revision=revision),
                                       name, ceiling_change=True))

    return mcp
