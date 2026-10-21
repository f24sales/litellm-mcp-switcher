"""Curated MCP management tools with per-call identity and role checks."""
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier, require_scopes
from fastmcp.server.dependencies import get_access_token

from .config import authenticate
from .models import ApplyInput, Problem, ProjectInput, Selection


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

    @mcp.tool(auth=require_scopes("user"))
    async def current_state() -> dict:
        """Read your allowed MCP servers, active selection, projects and state revision."""
        return await run(service.state(actor()))

    @mcp.tool(auth=require_scopes("user"))
    async def discover_tools(server_id: str) -> dict:
        """Discover selectable tools of one permitted server without invoking them."""
        return await run(service.tools(actor(), server_id))

    @mcp.tool(auth=require_scopes("user"))
    async def save_project(name: str, selection: Selection, project_id: str | None = None,
                           version: int | None = None) -> dict:
        """Create or update your project. Saving does not activate it. Updates require its version."""
        data = ProjectInput(name=name, selection=selection, version=version)
        return await run(service.save_project(actor(), data, project_id))

    @mcp.tool(auth=require_scopes("user"))
    async def delete_project(project_id: str, version: int) -> dict:
        """Delete your saved project using its current version. Active access is unchanged."""
        try:
            return service.delete_project(actor(), project_id, version)
        except Problem as exc:
            raise ToolError(exc.message) from None

    @mcp.tool(auth=require_scopes("user"))
    async def apply_selection(selection: Selection, revision: str) -> dict:
        """Activate your complete MCP selection. Obtain revision from current_state first.

        An empty servers object disables optional MCPs. Configured always-on MCPs and
        all their tools are retained. This affects every client using your assigned
        LiteLLM key. It never changes LLM settings.
        """
        return await run(service.apply(actor(), ApplyInput(selection=selection, revision=revision)))

    @mcp.tool(auth=require_scopes("admin"))
    async def admin_list_users() -> dict:
        """List configured Switcher users without exposing their credentials."""
        actor(True)
        return {"users": sorted(service.users)}

    @mcp.tool(auth=require_scopes("admin"))
    async def admin_inspect_user(name: str) -> dict:
        """Read a user's ceiling, active selection, registry and current revision."""
        return await run(service.state(actor(True), name))

    @mcp.tool(auth=require_scopes("admin"))
    async def admin_discover_tools(name: str, server_id: str) -> dict:
        """Discover tools for defining a user's administrative ceiling."""
        return await run(service.tools(actor(True), server_id, name))

    @mcp.tool(auth=require_scopes("admin"))
    async def admin_set_access(name: str, selection: Selection, revision: str) -> dict:
        """Replace a user's administrative MCP ceiling and cap their active selection.

        This can grant registry servers to the user. Empty servers revokes all MCP access.
        Requires the revision returned by admin_inspect_user.
        """
        return await run(service.apply(actor(True), ApplyInput(selection=selection, revision=revision),
                                       name, ceiling_change=True))

    return mcp
