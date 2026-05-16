"""Static web helpers extracted from server.py."""

from __future__ import annotations

import importlib.util
import pathlib

from starlette.responses import HTMLResponse, FileResponse
from starlette.routing import Route
from starlette.staticfiles import StaticFiles


def resolve_web_dir(repo_dir: pathlib.Path) -> pathlib.Path:
    repo_web_dir = repo_dir / "web"
    if repo_web_dir.exists():
        return repo_web_dir

    spec = importlib.util.find_spec("web")
    origin = getattr(spec, "origin", None) if spec else None
    if origin:
        package_dir = pathlib.Path(origin).resolve().parent
        if package_dir.exists():
            return package_dir

    return repo_web_dir


class NoCacheStaticFiles:
    """Wrap StaticFiles to add Cache-Control: no-cache headers."""

    def __init__(self, **kwargs):
        self._app = StaticFiles(**kwargs)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            async def send_with_no_cache(message):
                if message["type"] == "http.response.start":
                    headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-cache, must-revalidate"))
                    message = {**message, "headers": headers}
                await send(message)

            await self._app(scope, receive, send_with_no_cache)
        else:
            await self._app(scope, receive, send)


def make_index_page(web_dir: pathlib.Path):
    async def index_page(_request) -> FileResponse | HTMLResponse:
        index = web_dir / "index.html"
        if index.exists():
            return FileResponse(str(index), media_type="text/html")
        return HTMLResponse("<html><body><h1>Ouroboros — web/ not found</h1></body></html>", status_code=404)

    return index_page


def make_pwa_routes(web_dir: pathlib.Path) -> list[Route]:
    """Serve manifest and service worker from site root (required PWA scope)."""

    async def manifest(_request) -> FileResponse | HTMLResponse:
        path = web_dir / "manifest.webmanifest"
        if path.exists():
            return FileResponse(str(path), media_type="application/manifest+json")
        return HTMLResponse("manifest not found", status_code=404)

    async def service_worker(_request) -> FileResponse | HTMLResponse:
        path = web_dir / "sw.js"
        if path.exists():
            return FileResponse(
                str(path),
                media_type="application/javascript",
                headers={"Service-Worker-Allowed": "/"},
            )
        return HTMLResponse("service worker not found", status_code=404)

    return [
        Route("/manifest.webmanifest", endpoint=manifest),
        Route("/sw.js", endpoint=service_worker),
    ]
