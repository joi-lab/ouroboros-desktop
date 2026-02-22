"""
Ouroboros — Local model lifecycle manager.

Manages downloading, starting, stopping, and health-checking a local
llama-cpp-python server for on-device LLM inference with Metal + mmap.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

_LOCAL_MODEL_DEFAULT_PORT = 8766

# Global singleton — one local model server at a time
_manager: Optional[LocalModelManager] = None
_manager_lock = threading.Lock()


def get_manager() -> LocalModelManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LocalModelManager()
        return _manager


class LocalModelManager:
    """Lifecycle manager for a llama-cpp-python server subprocess."""

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._status = "offline"
        self._error: Optional[str] = None
        self._model_path: Optional[str] = None
        self._port: int = _LOCAL_MODEL_DEFAULT_PORT
        self._context_length: int = 0
        self._model_name: str = ""
        self._download_progress: float = 0.0

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    def get_status(self) -> str:
        if self._proc is not None and self._proc.poll() is not None:
            self._status = "error"
            self._error = f"Server exited with code {self._proc.returncode}"
            self._proc = None
        return self._status

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_running(self) -> bool:
        return self.get_status() == "ready"

    def status_dict(self) -> Dict[str, Any]:
        return {
            "status": self.get_status(),
            "error": self._error,
            "model_path": self._model_path,
            "model_name": self._model_name,
            "context_length": self._context_length,
            "port": self._port,
            "download_progress": self._download_progress,
        }

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_model(
        self,
        source: str,
        filename: str = "",
        progress_cb: Optional[Callable[[float], None]] = None,
    ) -> str:
        """Download a model from HuggingFace or resolve a local path.

        Args:
            source: HF repo ID (e.g. "bartowski/Llama-3.3-70B-Instruct-GGUF")
                    or absolute path to a .gguf file.
            filename: Specific file within the HF repo (required for HF repos).
            progress_cb: Optional callback(fraction) for download progress.

        Returns:
            Absolute path to the downloaded/resolved .gguf file.
        """
        if os.path.isfile(source):
            log.info("Using local model file: %s", source)
            return source

        if source.startswith("/") or source.startswith("~"):
            expanded = os.path.expanduser(source)
            if os.path.isfile(expanded):
                return expanded
            raise FileNotFoundError(f"Local model file not found: {expanded}")

        # HuggingFace download
        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            raise RuntimeError(
                "huggingface_hub is required for downloading models. "
                "Install with: pip install huggingface_hub"
            )

        if not filename:
            raise ValueError(
                "filename is required when source is a HuggingFace repo ID. "
                "Example: filename='model-Q4_K_M.gguf'"
            )

        self._status = "downloading"
        self._download_progress = 0.0
        log.info("Downloading %s/%s from HuggingFace...", source, filename)

        try:
            path = hf_hub_download(
                repo_id=source,
                filename=filename,
                resume_download=True,
            )
            self._download_progress = 1.0
            if progress_cb:
                progress_cb(1.0)
            log.info("Model downloaded to: %s", path)
            return path
        except Exception as e:
            self._status = "error"
            self._error = f"Download failed: {e}"
            raise

    # ------------------------------------------------------------------
    # Start / Stop
    # ------------------------------------------------------------------

    def start_server(
        self,
        model_path: str,
        port: int = _LOCAL_MODEL_DEFAULT_PORT,
        n_gpu_layers: int = -1,
        n_ctx: int = 0,
        chat_format: str = "chatml-function-calling",
    ) -> None:
        """Start the llama-cpp-python server as a subprocess."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RuntimeError("Local model server is already running")

            self._model_path = model_path
            self._port = port
            self._status = "loading"
            self._error = None

            python = sys.executable
            cmd = [
                python, "-m", "llama_cpp.server",
                "--model", model_path,
                "--port", str(port),
                "--n_gpu_layers", str(n_gpu_layers),
                "--chat_format", chat_format,
            ]
            if n_ctx > 0:
                cmd.extend(["--n_ctx", str(n_ctx)])

            log.info("Starting local model server: %s", " ".join(cmd))

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                self._status = "error"
                self._error = (
                    "llama-cpp-python not found. Install with:\n"
                    'CMAKE_ARGS="-DGGML_METAL=on" pip install llama-cpp-python[server]'
                )
                raise RuntimeError(self._error)

        # Wait for server to become healthy in a background thread
        threading.Thread(
            target=self._wait_for_healthy, daemon=True, name="local-model-health"
        ).start()

    def _wait_for_healthy(self, timeout: float = 120.0) -> None:
        """Poll the server until it responds or times out."""
        start = time.time()
        while time.time() - start < timeout:
            if self._proc is None or self._proc.poll() is not None:
                self._status = "error"
                rc = self._proc.returncode if self._proc else "?"
                self._error = f"Server process exited during startup (code {rc})"
                return
            try:
                health = self.health_check()
                if health.get("ok"):
                    self._status = "ready"
                    self._context_length = health.get("context_length", 0)
                    self._model_name = health.get("model_name", "")
                    log.info(
                        "Local model server ready (ctx=%d, model=%s)",
                        self._context_length, self._model_name,
                    )
                    return
            except Exception:
                pass
            time.sleep(2.0)

        self._status = "error"
        self._error = f"Server failed to become healthy within {timeout}s"
        log.error(self._error)

    def stop_server(self) -> None:
        """Stop the local model server subprocess."""
        with self._lock:
            proc = self._proc
            self._proc = None
            self._status = "offline"
            self._error = None
            self._context_length = 0
            self._model_name = ""

        if proc is None:
            return

        log.info("Stopping local model server (pid=%s)...", proc.pid)
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("Local model server did not exit, sending SIGKILL")
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    # ------------------------------------------------------------------
    # Health & Info
    # ------------------------------------------------------------------

    def health_check(self) -> Dict[str, Any]:
        """Query the local server for health and model info."""
        import requests

        url = f"http://127.0.0.1:{self._port}/v1/models"
        resp = requests.get(url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data", [])
        if not models:
            return {"ok": False, "error": "No models loaded"}

        model_info = models[0]
        ctx = model_info.get("meta", {}).get("n_ctx_train", 0)
        if not ctx:
            ctx = model_info.get("context_window", 0)

        return {
            "ok": True,
            "model_name": model_info.get("id", "unknown"),
            "context_length": ctx,
        }

    def get_context_length(self) -> int:
        """Return cached context length, or query the server."""
        if self._context_length > 0:
            return self._context_length
        try:
            info = self.health_check()
            self._context_length = info.get("context_length", 4096)
        except Exception:
            self._context_length = 4096
        return self._context_length

    # ------------------------------------------------------------------
    # Tool calling test
    # ------------------------------------------------------------------

    def test_tool_calling(self) -> Dict[str, Any]:
        """Run a basic tool call test against the local server.

        Returns dict with: success, chat_ok, tool_call_ok, details, tokens_per_sec.
        """
        from openai import OpenAI

        client = OpenAI(
            base_url=f"http://127.0.0.1:{self._port}/v1",
            api_key="local",
        )

        result: Dict[str, Any] = {
            "success": False,
            "chat_ok": False,
            "tool_call_ok": False,
            "details": "",
            "tokens_per_sec": 0.0,
        }

        # Test 1: basic chat
        try:
            t0 = time.time()
            resp = client.chat.completions.create(
                model="local-model",
                messages=[{"role": "user", "content": "Say hello in one word."}],
                max_tokens=32,
            )
            elapsed = time.time() - t0
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            tokens = resp.usage.completion_tokens if resp.usage else len(text.split())
            result["chat_ok"] = bool(text.strip())
            if elapsed > 0 and tokens > 0:
                result["tokens_per_sec"] = round(tokens / elapsed, 1)
        except Exception as e:
            result["details"] = f"Basic chat failed: {e}"
            return result

        # Test 2: tool calling
        try:
            tools = [{
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Returns the current time.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }]
            resp = client.chat.completions.create(
                model="local-model",
                messages=[{"role": "user", "content": "What time is it? Use the get_time tool."}],
                tools=tools,
                tool_choice="auto",
                max_tokens=256,
            )
            msg = resp.choices[0].message if resp.choices else None
            if msg and msg.tool_calls:
                result["tool_call_ok"] = True
            else:
                result["details"] = "Model returned text instead of tool_call"
        except Exception as e:
            result["details"] = f"Tool call test failed: {e}"
            result["success"] = result["chat_ok"]
            return result

        result["success"] = result["chat_ok"] and result["tool_call_ok"]
        if result["success"]:
            result["details"] = "All tests passed"
        elif result["chat_ok"] and not result["tool_call_ok"]:
            result["details"] = (
                "Chat works but tool calling failed. "
                "This model may not work for main agent tasks. "
                "Consider using it for Light/Consciousness only."
            )
        return result
