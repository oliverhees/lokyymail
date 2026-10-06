"""App-Fabrik. Startet Weboberfläche, Geräte-Schnittstelle und MCP-Server in einem Prozess."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings

from .. import __version__
from ..config import get_settings
from ..db import create_all, init_engine
from ..security import _master_key
from .deps import LoginRequired, templates
from .routes_device import router as device_router
from .routes_ui import router as ui_router

_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "frame-src 'self'; frame-ancestors 'none'; form-action 'self' https://accounts.google.com; "
    "base-uri 'none'; object-src 'none'"
)


def _is_api(request: Request) -> bool:
    return request.url.path.startswith(("/api/", "/mcp", "/health"))


class _McpKeyGate:
    """Weist jede MCP-Anfrage ohne gültigen KI-Schlüssel schon auf HTTP-Ebene ab (401/403)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            import anyio

            from ..db import session_scope
            from .deps import resolve_api_key

            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")

            def check() -> tuple[int, str]:
                with session_scope() as db:
                    try:
                        resolve_api_key(db, auth, kind="ai")
                        return 200, ""
                    except HTTPException as exc:
                        return exc.status_code, str(exc.detail)

            status, detail = await anyio.to_thread.run_sync(check)
            if status != 200:
                body = json.dumps({"detail": "Gültiger LokyyMail-KI-Schlüssel nötig. " + detail}).encode()
                await send({"type": "http.response.start", "status": status,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def create_app(*, init_db: bool = True) -> FastAPI:
    settings = get_settings()
    _master_key()  # Fehlt der Hauptschlüssel, startet die App gar nicht erst.

    from ..mcp_server import mcp

    allowed_hosts = sorted({settings.public_host, "localhost:*", "127.0.0.1:*", *settings.mcp_extra_hosts})
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[settings.public_url.rstrip("/")],
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if init_db:
            init_engine()
            create_all()
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="LokyyMail", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(ui_router)
    app.include_router(device_router)
    for route in mcp_app.routes:
        route.app = _McpKeyGate(route.app)  # zusätzliche Schicht: ohne gültigen KI-Schlüssel kein Zugang
        app.router.routes.append(route)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not _is_api(request):
            response.headers.setdefault("Content-Security-Policy", _CSP)
            response.headers.setdefault("Cache-Control", "no-store")
        if settings.secure_cookies:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        return RedirectResponse(exc.target, status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if _is_api(request):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(
            request, "error.html", {"user": None, "csrf": "", "status": exc.status_code, "message": exc.detail}, status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if _is_api(request):
            return JSONResponse({"detail": "Ungültige Eingabe.", "errors": jsonable_encoder(exc.errors()[:5])}, status_code=422)
        return templates.TemplateResponse(
            request, "error.html", {"user": None, "csrf": "", "status": 422, "message": "Ungültige Eingabe."}, status_code=422
        )

    return app
