"""FastAPI endpoints; the browser retains a bearer only in page memory."""
from pathlib import Path
from typing import Literal
import re

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .aliases import alias_revision
from .config import authenticate
from .models import AliasInput, ApplyInput, Problem, ProjectInput, RevisionInput, RotateInput, ToggleInput


def create_web(service, identities):
    app = FastAPI(title="LiteLLM MCP Switcher", version="2026.10.3", docs_url=None, redoc_url=None)
    package = Path(__file__).parent
    app.mount("/static", StaticFiles(directory=package / "static"), name="static")
    templates = Jinja2Templates(directory=package / "templates")
    security = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    def actor(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        identity = authenticate(credentials.credentials if credentials else "", identities)
        if identity is None:
            raise Problem(401, "A configured Switcher bearer is required")
        return identity

    def selected(key_name: str | None = Query(default=None), user=Depends(actor)):
        if user.admin:
            raise Problem(403, "Use the administrative endpoints for administrative access")
        return service.target(user, key_name)

    @app.get("/", include_in_schema=False)
    async def home(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={})

    @app.get("/healthz", include_in_schema=False)
    async def health():
        # Listener readiness is local; upstream failures are shown through authenticated discovery.
        return {"status": "ok", "service": "litellm-mcp-switcher"}

    @app.get("/icons/{filename}", include_in_schema=False)
    async def icon(filename: str):
        path = Path(service.settings.icon_directory) / filename
        if not re.fullmatch(r"[0-9a-f]{64}\.png", filename) or not path.is_file():
            raise Problem(404, "No icon")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/me")
    async def me(user=Depends(actor)):
        return {"name": user.name, "admin": user.admin,
                "orchestrator": user.orchestrator, "key_names": list(user.key_names),
                "refresh_seconds": service.settings.webui_refresh_seconds}

    @app.get("/api/state")
    async def state(key_name: str | None = None, user=Depends(actor)):
        if user.admin:
            raise Problem(403, "Choose an administrative target")
        return await service.state(user, key_name, show_catalog=True)

    @app.put("/api/targets/{name}/servers/{sid}/active")
    async def toggle(name: str, sid: str, data: ToggleInput, user=Depends(actor)):
        return await service.toggle(user, name, sid, data)

    @app.put("/api/admin/users/{name}/permissions")
    async def save(name: str, data: RevisionInput, user=Depends(actor)):
        return await service.save_permissions(user, name, data.revision)

    @app.get("/api/servers/{server_id}/tools")
    async def tools(server_id: str, refresh: bool = False, user=Depends(selected)):
        return await service.tools(user, server_id, refresh=refresh)

    @app.post("/api/projects", status_code=201)
    async def create_project(data: ProjectInput, user=Depends(selected)):
        return await service.save_project(user, data)

    @app.put("/api/projects/{project_id}")
    async def update_project(project_id: str, data: ProjectInput, user=Depends(selected)):
        return await service.save_project(user, data, project_id)

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, version: int = Query(ge=1), user=Depends(selected)):
        return service.delete_project(user, project_id, version)

    @app.post("/api/apply")
    async def apply(data: ApplyInput, user=Depends(selected)):
        return await service.apply(user, data)

    def admin(user):
        if not user.admin:
            raise Problem(403, "Administrative access required")

    @app.post("/api/admin/users")
    async def create_alias(data: AliasInput, user=Depends(actor)):
        return await service.aliases.change(user, "create", data.name)

    @app.put("/api/admin/users/{name}/name")
    async def rename_alias(name: str, data: AliasInput, revision: str, user=Depends(actor)):
        return await service.aliases.change(user, "rename", name, data.name, revision)

    @app.delete("/api/admin/users/{name}")
    async def delete_alias(name: str, revision: str, user=Depends(actor)):
        return await service.aliases.change(user, "delete", name, revision=revision)

    @app.post("/api/admin/users/{name}/login")
    async def issue_login(name: str, revision: str, user=Depends(actor)):
        return await service.aliases.change(user, "login", name, revision=revision)

    @app.delete("/api/admin/users/{name}/login")
    async def delete_login(name: str, revision: str, user=Depends(actor)):
        return await service.aliases.change(user, "delete_login", name, revision=revision)

    @app.post("/api/admin/users/{name}/credentials/{kind}")
    async def reveal(name: str, kind: Literal["virtual", "switcher"], user=Depends(actor)):
        admin(user)
        target = service.target(user, name)
        value = target.litellm_key if kind == "virtual" else target.bearer
        if not value:
            raise Problem(404, "No personal Switcher login has been created")
        return {"value": value}

    @app.post("/api/admin/users/{name}/rotation/preview")
    async def preview_rotation(name: str, user=Depends(actor)):
        admin(user)
        return await service.rotation.execute(user, name, "preview")

    @app.post("/api/admin/users/{name}/rotation")
    async def rotate(name: str, data: RotateInput, user=Depends(actor)):
        admin(user)
        return await service.rotation.execute(user, name, "rotate", data.revision, data.operation_id)

    @app.post("/api/admin/tool-catalog/refresh", status_code=202)
    async def refresh_catalog(user=Depends(actor)):
        admin(user)
        service.tool_catalog.refresh_all()
        return {"refreshing": True}

    @app.get("/api/admin/users")
    async def users(user=Depends(actor)):
        admin(user)
        return {"users": sorted(service.users), "revisions": {n: alias_revision(u) for n, u in service.users.items()}}

    @app.get("/api/admin/users/{name}")
    async def user_state(name: str, user=Depends(actor)):
        admin(user)
        return await service.state(user, name)

    @app.get("/api/admin/users/{name}/servers/{server_id}/tools")
    async def admin_tools(name: str, server_id: str, refresh: bool = False, user=Depends(actor)):
        admin(user)
        return await service.tools(user, server_id, name, refresh)

    @app.put("/api/admin/users/{name}/access")
    async def access(name: str, data: ApplyInput, user=Depends(actor)):
        admin(user)
        return await service.apply(user, data, name, ceiling_change=True)

    return app
