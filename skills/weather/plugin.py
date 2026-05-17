"""Weather extension — Phase 5/v5 reference visual widget.

Registers the three PluginAPI v1 surfaces a real visual extension uses:

- ``register_route`` — ``GET /api/extensions/weather/forecast?city=...``
  fetches a compact summary from ``wttr.in`` and returns JSON.
- ``register_tool`` — ``ext_9_r_weather_fetch`` exposes the same call to
  the agent dispatcher (so the LLM can ask for weather without going
  through ``skill_exec``).
- ``register_ui_tab`` — declares a Widgets-page UI declaration so the runtime knows
  how to render the widget on the top-level Widgets page.

Every byte that runs on a request is in this file; no third-party
libraries (the ``net`` permission is bounded to ``wttr.in`` by host
allowlist + scheme allowlist + redirect refusal).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse


_ALLOWED_HOST = "wttr.in"
_TIMEOUT_SEC = 10
_USER_AGENT = "Ouroboros-Weather/0.2"


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse cross-host redirects so a misbehaving wttr.in mirror cannot
    pivot the request to an attacker-controlled host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urlparse(newurl).hostname
        if target != _ALLOWED_HOST:
            raise urllib.error.URLError(
                f"weather: cross-host redirect refused: {target!r} not in allowlist"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_StrictRedirectHandler())


def _fetch(city: str) -> Dict[str, Any]:
    """Resolve current conditions for ``city``.

    Returns a structured dict suitable for both the route handler
    (JSONResponse) and the tool handler (json.dumps). All network
    failures convert to an ``error`` field; the caller decides how to
    surface them.
    """
    cleaned = (city or "").strip()
    if not cleaned:
        return {"error": "city is empty"}
    if len(cleaned) > 80:
        return {"error": "city is too long"}
    url = f"https://{_ALLOWED_HOST}/{urllib.parse.quote(cleaned)}?format=j1"
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != _ALLOWED_HOST:
        # Defensive — quote() is safe but a rule of three is cheap.
        return {"error": f"refusing host {parsed.netloc!r}"}
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_SEC) as response:
            raw = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"error": f"upstream HTTP {exc.code}: {exc.reason}"}
    except urllib.error.URLError as exc:
        return {"error": f"network: {exc.reason!r}"}
    except TimeoutError:
        return {"error": f"upstream timed out after {_TIMEOUT_SEC}s"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"error": "upstream returned non-JSON payload"}
    current = (data.get("current_condition") or [{}])[0]
    nearest = (data.get("nearest_area") or [{}])[0]
    return {
        "city": cleaned,
        "resolved_to": (nearest.get("areaName") or [{}])[0].get("value", "") if nearest else "",
        "country": (nearest.get("country") or [{}])[0].get("value", "") if nearest else "",
        "temp_c": _coerce_int(current.get("temp_C")),
        "feels_like_c": _coerce_int(current.get("FeelsLikeC")),
        "humidity_pct": _coerce_int(current.get("humidity")),
        "condition": (current.get("weatherDesc") or [{}])[0].get("value") or "Unknown",
        "wind_kph": _coerce_int(current.get("windspeedKmph")),
        "wind_dir": str(current.get("winddir16Point") or "").strip(),
        "icon_code": str(current.get("weatherCode") or "").strip(),
        "observation_time": str(current.get("observation_time") or "").strip(),
    }


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


async def _route_forecast(request: Request) -> JSONResponse:
    """Handler for ``GET /api/extensions/weather/forecast?city=...``.

    ``_fetch`` performs a synchronous ``urllib.request.urlopen`` call,
    which would block the Uvicorn event loop for up to 10 seconds (the
    fetcher timeout) if invoked directly from the coroutine. v5 Cycle 1
    Gemini critic Finding 1 flagged this as a real DoS vector — a slow
    wttr.in or repeated requests from the UI/agent could serialise the
    worker. We dispatch to ``asyncio.to_thread`` so the blocking call
    runs in the default thread pool while the event loop stays
    responsive for `/api/state`, websocket frames, etc.
    """
    import asyncio
    city = (request.query_params.get("city") or "").strip()
    if not city:
        return JSONResponse({"error": "missing city query parameter"}, status_code=400)
    payload = await asyncio.to_thread(_fetch, city)
    status = 200 if "error" not in payload else 502
    return JSONResponse(payload, status_code=status)


def _tool_fetch(*, city: str = "") -> str:
    """Agent-callable tool. Returns a JSON string for the chat surface."""
    payload = _fetch(city)
    return json.dumps(payload, ensure_ascii=False)


def register(api: Any) -> None:
    """PluginAPI v1 entry point.

    The runtime calls this exactly once per load; the loader unloads
    every registration on disable / re-review by content-hash mismatch.
    """
    api.register_tool(
        "fetch",
        _tool_fetch,
        description=(
            "Fetch the current weather for a city via the public wttr.in service. "
            "Returns JSON with temperature, condition, humidity, and wind."
        ),
        schema={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name to look up (e.g., 'Moscow', 'Tokyo').",
                },
            },
            "required": ["city"],
        },
        timeout_sec=15,
    )
    api.register_route(
        "forecast",
        _route_forecast,
        methods=("GET",),
    )
    api.register_ui_tab(
        "widget",
        "Weather widget",
        icon="cloud",
        render={
            "kind": "declarative",
            "schema_version": 1,
            "components": [
                {
                    "type": "form",
                    "route": "forecast",
                    "method": "GET",
                    "target": "result",
                    "submit_label": "Refresh",
                    "fields": [
                        {
                            "name": "city",
                            "label": "City",
                            "type": "text",
                            "default": "Moscow",
                            "required": True,
                        },
                    ],
                },
                {
                    "type": "status",
                    "target": "result",
                    "idle": "Enter a city and press Refresh.",
                    "loading": "Loading...",
                    "error": "Weather lookup failed.",
                    "success": "Latest conditions",
                },
                {
                    "type": "kv",
                    "target": "result",
                    "fields": [
                        {"label": "City", "path": "resolved_to"},
                        {"label": "Temperature", "path": "temp_c"},
                        {"label": "Feels like", "path": "feels_like_c"},
                        {"label": "Condition", "path": "condition"},
                        {"label": "Humidity", "path": "humidity_pct"},
                        {"label": "Wind speed", "path": "wind_kph"},
                        {"label": "Wind direction", "path": "wind_dir"},
                    ],
                },
            ],
        },
    )
    api.log("info", "weather: extension registered (route, tool, ui_tab)")


__all__ = ["register"]
