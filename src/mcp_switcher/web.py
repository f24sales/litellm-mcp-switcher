"""FastAPI endpoints; the browser retains a bearer only in page memory."""
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import authenticate
from .models import ApplyInput, Problem, ProjectInput


def create_web(service, identities):
    app = FastAPI(title="LiteLLM MCP Switcher", version="2026.10.1", docs_url=None, redoc_url=None)
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

    @app.get("/", include_in_schema=False)
    async def home(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={})

    @app.get("/healthz", include_in_schema=False)
    async def health():
        # Listener readiness is local; upstream failures are shown through authenticated discovery.
        return {"status": "ok", "service": "litellm-mcp-switcher"}

    @app.get("/api/me")
    async def me(user=Depends(actor)):
        return {"name": user.name, "admin": user.admin,
                "refresh_seconds": service.settings.webui_refresh_seconds}

    @app.get("/api/state")
    async def state(user=Depends(actor)):
        return await service.state(user)

    @app.get("/api/servers/{server_id}/tools")
    async def tools(server_id: str, refresh: bool = False, user=Depends(actor)):
        return await service.tools(user, server_id, refresh=refresh)

    @app.post("/api/projects", status_code=201)
    async def create_project(data: ProjectInput, user=Depends(actor)):
        return await service.save_project(user, data)

    @app.put("/api/projects/{project_id}")
    async def update_project(project_id: str, data: ProjectInput, user=Depends(actor)):
        return await service.save_project(user, data, project_id)

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, version: int = Query(ge=1), user=Depends(actor)):
        return service.delete_project(user, project_id, version)

    @app.post("/api/apply")
    async def apply(data: ApplyInput, user=Depends(actor)):
        return await service.apply(user, data)

    def admin(user):
        if not user.admin:
            raise Problem(403, "Administrative access required")

    @app.get("/api/admin/users")
    async def users(user=Depends(actor)):
        admin(user)
        return {"users": sorted(service.users)}

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
