"""
Ouroboros — Local model lifecycle manager.

Manages downloading, starting, stopping, and health-checking a local
llama-cpp-python server for on-device LLM inference.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

from ouroboros.compat import IS_MACOS, terminate_process_tree, kill_process_tree

log = logging.getLogger(__name__)

_LOCAL_MODEL_DEFAULT_PORT = 8766

# Windows: prevent console windows when spawning subprocesses from the GUI app.
_SUBPROCESS_NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
)


def _with_hidden_subprocess(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    if _SUBPROCESS_NO_WINDOW:
        kwargs = dict(kwargs)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _SUBPROCESS_NO_WINDOW
    return kwargs

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
        self._stderr_buf: bytes = b""

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
        chat_format: str = "",
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
            ]
            if chat_format:
                cmd.extend(["--chat_format", chat_format])
            effective_ctx = n_ctx if n_ctx > 0 else 16384
            self._context_length = effective_ctx
            cmd.extend(["--n_ctx", str(effective_ctx)])

            log.info("Starting local model server: %s", " ".join(cmd))

            try:
                probe = subprocess.run(
                    [python, "-c", "import llama_cpp"],
                    **_with_hidden_subprocess({
                        "capture_output": True,
                        "text": True,
                        "timeout": 15,
                    }),
                )
            except Exception as exc:
                self._status = "error"
                self._error = f"Failed to verify llama-cpp-python installation: {exc}"
                raise RuntimeError(self._error) from exc
            if probe.returncode != 0:
                self._status = "error"
                details = (probe.stderr or probe.stdout or "").strip()
                if IS_MACOS:
                    hint = 'CMAKE_ARGS="-DGGML_METAL=on" pip install llama-cpp-python[server]'
                else:
                    hint = "pip install llama-cpp-python[server]"
                self._error = f"llama-cpp-python is not installed or failed to import. Install with: {hint}"
                if details:
                    self._error += f": {details[-500:]}"
                raise RuntimeError(self._error)

            try:
                _popen_kwargs = dict(
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                )
                if sys.platform == "win32":
                    _popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                else:
                    _popen_kwargs["start_new_session"] = True
                self._proc = subprocess.Popen(cmd, **_with_hidden_subprocess(_popen_kwargs))
            except FileNotFoundError:
                self._status = "error"
                self._error = "Python executable not found. Cannot start local model server."
                raise RuntimeError(self._error)

            self._stderr_buf = b""
            threading.Thread(
                target=self._drain_stderr, daemon=True, name="local-model-stderr"
            ).start()

        # Wait for server to become healthy in a background thread
        threading.Thread(
            target=self._wait_for_healthy, daemon=True, name="local-model-health"
        ).start()

    def _drain_stderr(self) -> None:
        """Continuously read stderr to prevent pipe buffer deadlock.

        Keeps the last 2 KB in self._stderr_buf for error diagnostics.
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        buf = b""
        try:
            fd = proc.stderr.fileno()
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                buf = (buf + chunk)[-2048:]
        except Exception:
            pass
        self._stderr_buf = buf

    def _wait_for_healthy(self, timeout: float = 300.0) -> None:
        """Poll the server until it responds or times out."""
        start = time.time()
        while time.time() - start < timeout:
            if self._proc is None or self._proc.poll() is not None:
                self._status = "error"
                rc = self._proc.returncode if self._proc else "?"
                stderr_tail = ""
                if self._stderr_buf:
                    try:
                        stderr_tail = self._stderr_buf.decode("utf-8", errors="replace")[-500:]
                    except Exception:
                        pass
                self._error = f"Server process exited during startup (code {rc})"
                if stderr_tail:
                    self._error += f": {stderr_tail}"
                self._proc = None
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
            self._stderr_buf = b""

        if proc is None:
            return

        log.info("Stopping local model server (pid=%s)...", proc.pid)
        terminate_process_tree(proc)

        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("Local model server did not exit, force-killing")
            kill_process_tree(proc)
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
            tool_calls = list(getattr(msg, "tool_calls", None) or []) if msg else []
            if msg and not tool_calls and getattr(msg, "content", None):
                from ouroboros.llm import LLMClient

                parsed = LLMClient._parse_tool_calls_from_content(
                    {
                        "content": msg.content,
                        "tool_calls": [],
                    },
                    {"get_time"},
                )
                tool_calls = parsed.get("tool_calls") or []
            if tool_calls:
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
