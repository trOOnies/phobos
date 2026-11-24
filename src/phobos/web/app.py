"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from phobos import __version__, repo
from phobos.config import Settings, load_settings
from phobos.jobs import JobManager
from phobos.library import Library
from phobos.web.api import router

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
CSRF_HEADER = "x-phobos"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class DevStaticFiles(StaticFiles):
    """Static files that the browser always revalidates (cheap 304s on localhost),
    so edits to the JS/CSS show up on reload without cache-busting machinery."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


RELOAD_ENV = "PHOBOS_RELOAD"


def stop_server() -> None:
    """Ask uvicorn to shut down gracefully, as if Ctrl+C was pressed in its terminal.

    With ``--reload`` the app runs in a worker process under a reloader that would
    just keep waiting for file changes, so the reloader is stopped instead (it then
    stops the worker).
    """
    target = os.getppid() if os.environ.get(RELOAD_ENV) == "1" else os.getpid()
    os.kill(target, signal.SIGTERM)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        library = Library(settings)
        app.state.library = library
        app.state.jobs = JobManager(library)
        with library.conn() as conn:
            if stale := repo.interrupt_unfinished_imports(conn):
                log.info("marked %d unfinished import(s) from a previous run as interrupted", stale)
        for warning in settings.warnings():
            log.warning(warning)
        log.info("library: %s | data: %s", settings.library_dir, settings.data_dir)
        yield
        app.state.jobs.shutdown()

    app = FastAPI(title="Phobos", version=__version__, lifespan=lifespan)
    app.state.stop_server = stop_server  # swapped out in tests

    # The server only listens on localhost, but any web page open in the browser
    # can still send requests to it. Two cheap defences:
    #  * Host allowlist - blocks DNS-rebinding attacks.
    #  * A custom header on state-changing API calls - browsers won't send custom
    #    headers cross-origin without a CORS preflight, which we never approve.
    hosts = ["localhost", "127.0.0.1", "[::1]", "testserver", *settings.allowed_hosts]
    if settings.host not in ("127.0.0.1", "0.0.0.0", "localhost", "::"):
        hosts.append(settings.host)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def require_csrf_header(request: Request, call_next):
        if (
            request.method not in SAFE_METHODS
            and request.url.path.startswith("/api/")
            and request.headers.get(CSRF_HEADER) != "1"
        ):
            return JSONResponse({"detail": "missing X-Phobos header"}, status_code=403)
        return await call_next(request)

    app.include_router(router)
    app.mount("/static", DevStaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {"version": __version__})

    return app
