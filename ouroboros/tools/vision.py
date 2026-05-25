"""Vision LLM tools for browser screenshots and uploaded images."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from ouroboros.config import SETTINGS_DEFAULTS
from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_DEFAULT_VLM_MODEL = SETTINGS_DEFAULTS["OUROBOROS_MODEL"]


def _get_llm_client():
    """Lazy-import LLMClient to avoid circular imports."""
    from ouroboros.llm import LLMClient
    return LLMClient()


def _analyze_screenshot(ctx: ToolContext, prompt: str = "Describe what you see in this screenshot. Note any important UI elements, text, errors, or visual issues.", model: str = "") -> str:
    """Analyze the last browser screenshot via VLM."""
    b64 = ctx.browser_state.last_screenshot_b64
    if not b64:
        return (
            "⚠️ No screenshot available. "
            "First call browse_page(output='screenshot') or browser_action(action='screenshot')."
        )

    vlm_model = model or os.environ.get("OUROBOROS_MODEL", _DEFAULT_VLM_MODEL)

    try:
        client = _get_llm_client()
        text, usage = client.vision_query(
            prompt=prompt,
            images=[{"base64": b64, "mime": "image/png"}],
            model=vlm_model,
        )

        _emit_usage(ctx, usage, vlm_model)

        return text or "(no response from VLM)"
    except Exception as e:
        log.warning("analyze_screenshot failed: %s", e, exc_info=True)
        return f"⚠️ VLM analysis failed: {e}"


_IMAGE_MAGIC: List[tuple] = [
    (b'\x89PNG\r\n\x1a\n', "image/png"),
    (b'\xff\xd8\xff', "image/jpeg"),
    (b'GIF87a', "image/gif"),
    (b'GIF89a', "image/gif"),
]
_IMAGE_WEBP_MAGIC = (b'RIFF', b'WEBP')
_VLM_MAX_FILE_BYTES = 20 * 1024 * 1024


def _path_is_under(path: "pathlib.Path", root: "pathlib.Path") -> bool:
    """Return True if a resolved path is root itself or a descendant."""
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _detect_image_mime_for_vlm(raw: bytes) -> str:
    """Return MIME type string or empty string if not a recognised image."""
    for magic, mime in _IMAGE_MAGIC:
        if raw[:len(magic)] == magic:
            return mime
    if raw[:4] == _IMAGE_WEBP_MAGIC[0] and raw[8:12] == _IMAGE_WEBP_MAGIC[1]:
        return "image/webp"
    return ""


def _allowed_file_roots() -> List["pathlib.Path"]:
    """Return uploads roots allowed for VLM file_path reads."""
    import pathlib
    data_dir = os.environ.get("OUROBOROS_DATA_DIR", "")
    if data_dir:
        return [pathlib.Path(data_dir).expanduser().resolve() / "uploads"]
    return [pathlib.Path("~/Ouroboros/data/uploads").expanduser().resolve()]


def _vlm_query(ctx: ToolContext, prompt: str, image_url: str = "", image_base64: str = "", image_mime: str = "image/png", file_path: str = "", model: str = "") -> str:
    """Analyze one image from uploads file_path, public URL, or base64."""
    if not image_url and not image_base64 and not file_path:
        return "⚠️ Provide one of: file_path, image_url, or image_base64."

    images: List[Dict[str, Any]] = []
    if file_path:
        import base64
        import pathlib
        fp = pathlib.Path(file_path).expanduser().resolve()
        if not fp.exists():
            return f"⚠️ File not found: {file_path}"
        allowed = _allowed_file_roots()
        if not any(_path_is_under(fp, root) for root in allowed):
            return (
                f"⚠️ file_path must be inside the uploads directory (data/uploads/). "
                f"Resolved path: {fp}. Use send_photo or read_file for other paths."
            )
        if fp.stat().st_size > _VLM_MAX_FILE_BYTES:
            return f"⚠️ File too large ({fp.stat().st_size} bytes). Max {_VLM_MAX_FILE_BYTES} bytes."
        try:
            raw = fp.read_bytes()
        except Exception as e:
            return f"⚠️ Failed to read image file: {e}"
        # Fail closed: only recognized image bytes may reach the VLM.
        mime = _detect_image_mime_for_vlm(raw)
        if not mime:
            return (
                f"⚠️ File does not appear to be a supported image (PNG/JPEG/GIF/WEBP). "
                f"Only image files may be sent to the VLM via file_path."
            )
        images.append({"base64": base64.b64encode(raw).decode(), "mime": mime})
    elif image_url:
        images.append({"url": image_url})
    else:
        images.append({"base64": image_base64, "mime": image_mime})

    vlm_model = model or os.environ.get("OUROBOROS_MODEL", _DEFAULT_VLM_MODEL)

    try:
        client = _get_llm_client()
        text, usage = client.vision_query(
            prompt=prompt,
            images=images,
            model=vlm_model,
        )

        _emit_usage(ctx, usage, vlm_model)

        return text or "(no response from VLM)"
    except Exception as e:
        log.warning("vlm_query failed: %s", e, exc_info=True)
        return f"⚠️ VLM query failed: {e}"


def _emit_usage(ctx: ToolContext, usage: Dict[str, Any], model: str) -> None:
    """Emit LLM usage event for budget tracking."""
    if ctx.event_queue is None:
        return
    try:
        event = {
            "type": "llm_usage",
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": usage.get("cached_tokens", 0),
            "cost": usage.get("cost", 0.0),
            "task_id": ctx.task_id,
            "task_type": ctx.current_task_type or "task",
        }
        ctx.event_queue.put_nowait(event)
    except Exception:
        log.debug("Failed to emit VLM usage event", exc_info=True)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="analyze_screenshot",
            schema={
                "name": "analyze_screenshot",
                "description": (
                    "Analyze the last browser screenshot using a Vision LLM. "
                    "Must call browse_page(output='screenshot') or browser_action(action='screenshot') first. "
                    "Returns a text description and analysis of the screenshot. "
                    "Use this to verify UI, check for visual errors, or understand page layout."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "What to look for or analyze in the screenshot (default: general description)",
                        },
                        "model": {
                            "type": "string",
                            "description": "VLM model to use (default: current OUROBOROS_MODEL)",
                        },
                    },
                    "required": [],
                },
            },
            handler=_analyze_screenshot,
            timeout_sec=90,
        ),
        ToolEntry(
            name="vlm_query",
            schema={
                "name": "vlm_query",
                "description": (
                    "Analyze any image using a Vision LLM. "
                    "Provide one of: file_path (local file, preferred — avoids large base64 in arguments), "
                    "image_url (public URL), or image_base64 (base64-encoded PNG/JPEG). "
                    "Use file_path for files already on disk (e.g. data/uploads/ attachments). "
                    "Use for: analyzing charts, reading diagrams, understanding screenshots, checking UI."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "What to analyze or describe about the image",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Local file path to image (preferred — reads from disk, avoids base64 in arguments). Must be inside data/uploads/ directory.",
                        },
                        "image_url": {
                            "type": "string",
                            "description": "Public URL of the image to analyze",
                        },
                        "image_base64": {
                            "type": "string",
                            "description": "Base64-encoded image data",
                        },
                        "image_mime": {
                            "type": "string",
                            "description": "MIME type for base64 image (default: image/png)",
                        },
                        "model": {
                            "type": "string",
                            "description": "VLM model to use (default: current OUROBOROS_MODEL)",
                        },
                    },
                    "required": ["prompt"],
                },
            },
            handler=_vlm_query,
            timeout_sec=90,
        ),
    ]
