"""Curated MCP management tools with per-call identity and role checks."""
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier, require_scopes
from fastmcp.server.dependencies import get_access_token

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
                scopes=["admin" if user.admin else "user"], claims={"sub": user.name})

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

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": True})
    async def current_state() -> dict:
        """Read only: inspect your allowed MCP servers, active selection, projects and revision. Changes nothing."""
        return await run(service.state(actor()))

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": True})
    async def discover_tools(server_id: str) -> dict:
        """Read only: list selectable tools of one permitted server without invoking them or changing access."""
        return await run(service.tools(actor(), server_id))

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": False},
              description=CHANGE_POLICY + "\n\nCreate or update your saved project only on explicit request. "
              "Saving does not activate it. Updates require its version. Always-on MCPs retain all their tools.")
    async def save_project(name: str, selection: Selection, project_id: str | None = None,
                           version: int | None = None) -> dict:
        """Create or update your project. Saving does not activate it. Updates require its version."""
        data = ProjectInput(name=name, selection=selection, version=version)
        return await run(service.save_project(actor(), data, project_id))

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": False, "destructiveHint": True},
              description=CHANGE_POLICY + "\n\nDelete only the saved project the user explicitly asks to delete, "
              "using its current version. Active access is unchanged.")
    async def delete_project(project_id: str, version: int) -> dict:
        """Delete your saved project using its current version. Active access is unchanged."""
        try:
            return service.delete_project(actor(), project_id, version)
        except Problem as exc:
            raise ToolError(exc.message) from None

    @mcp.tool(auth=require_scopes("user"), annotations={"readOnlyHint": False, "destructiveHint": True},
              description=CHANGE_POLICY + "\n\nActivate only the MCP/tool selection explicitly requested by the user. "
              "Read current_state first and preserve every unrelated active selection unless the user explicitly "
              "requests its removal. This replaces the complete active selection for every client using this "
              "LiteLLM key. An empty servers object disables every optional MCP and requires an explicit request "
              "to do so. Always-on MCPs retain all their tools. Never changes LLM settings.")
    async def apply_selection(selection: Selection, revision: str) -> dict:
        """Activate your complete MCP selection. Obtain revision from current_state first.

        An empty servers object disables optional MCPs. Configured always-on MCPs and
        all their tools are retained. This affects every client using your assigned
        LiteLLM key. It never changes LLM settings.
        """
        return await run(service.apply(actor(), ApplyInput(selection=selection, revision=revision)))

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
