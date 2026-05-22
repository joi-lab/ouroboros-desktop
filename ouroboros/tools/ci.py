"""CI tools: trigger and monitor GitHub Actions workflows."""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.config import load_settings
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, run_cmd

log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_POLL_INTERVAL_SEC = 30
_MAX_POLL_SEC = 900  # 15 minutes


def _get_github_config() -> Tuple[str, str]:
    settings = load_settings()
    token = (settings.get("GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN", "")).strip()
    repo = (settings.get("GITHUB_REPO") or os.environ.get("GITHUB_REPO", "")).strip()
    if not token:
        raise ValueError("GITHUB_TOKEN not configured. Set it in Settings → Integrations.")
    if not repo:
        raise ValueError("GITHUB_REPO not configured. Set it in Settings → Integrations (format: owner/repo).")
    return token, repo


def _gh_api(method: str, path: str, token: str, body: Optional[dict] = None,
            timeout: int = 30) -> Tuple[int, dict]:
    url = f"{_GITHUB_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode()
            return resp.status, json.loads(content) if content.strip() else {}
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode()[:500]
        except Exception:
            pass
        return e.code, {"error": body_text}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": f"Network error: {e}"}


def _push_branch(repo_dir: str, branch: str) -> Tuple[bool, str]:
    try:
        result = run_cmd(["git", "push", "-u", "origin", branch], cwd=repo_dir)
        return True, result
    except Exception as e:
        return False, str(e)


def _get_current_branch(repo_dir: str) -> str:
    return run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir).strip()


def _get_current_sha(repo_dir: str) -> str:
    return run_cmd(["git", "rev-parse", "HEAD"], cwd=repo_dir).strip()


def _find_workflow_id(token: str, repo: str) -> Optional[int]:
    status, data = _gh_api("GET", f"/repos/{repo}/actions/workflows", token)
    if status != 200:
        return None
    for wf in data.get("workflows", []):
        if wf.get("path", "").endswith("ci.yml"):
            return wf["id"]
    return None


def _trigger_workflow(token: str, repo: str, workflow_id: int, branch: str) -> Tuple[bool, str]:
    status, data = _gh_api(
        "POST",
        f"/repos/{repo}/actions/workflows/{workflow_id}/dispatches",
        token,
        body={"ref": branch},
    )
    if status in (204, 200):
        return True, "Workflow dispatch triggered"
    return False, f"Failed to trigger (HTTP {status}): {data}"


def _poll_workflow_run(token: str, repo: str, branch: str, sha: str,
                       started_after: str, ctx: ToolContext,
                       timeout_sec: int = _MAX_POLL_SEC,
                       workflow_id: Optional[int] = None) -> dict:
    deadline = time.time() + timeout_sec
    run_id = None

    runs_path = (
        f"/repos/{repo}/actions/workflows/{workflow_id}/runs?branch={branch}&per_page=5&event=workflow_dispatch"
        if workflow_id
        else f"/repos/{repo}/actions/runs?branch={branch}&per_page=5&event=workflow_dispatch"
    )

    while time.time() < deadline:
        status, data = _gh_api("GET", runs_path, token)
        if status == 200:
            for run in data.get("workflow_runs", []):
                created = run.get("created_at", "")
                if created >= started_after and run.get("head_sha", "").startswith(sha[:7]):
                    run_id = run["id"]
                    run_status = run.get("status", "")
                    run_conclusion = run.get("conclusion")
                    run_url = run.get("html_url", "")

                    if run_status == "completed":
                        return {
                            "status": "completed",
                            "conclusion": run_conclusion,
                            "url": run_url,
                            "run_id": run_id,
                        }
                    _emit_progress(ctx, f"⏳ CI running... status={run_status} (polling every {_POLL_INTERVAL_SEC}s)")
                    break

        if not run_id:
            _emit_progress(ctx, "⏳ Waiting for CI run to appear...")

        time.sleep(_POLL_INTERVAL_SEC)

    return {"status": "timeout", "conclusion": None, "url": "", "run_id": run_id}


def _get_failed_jobs(token: str, repo: str, run_id: int) -> List[dict]:
    status, data = _gh_api("GET", f"/repos/{repo}/actions/runs/{run_id}/jobs", token)
    if status != 200:
        return []
    failed = []
    for job in data.get("jobs", []):
        if job.get("conclusion") == "failure":
            failed_steps = [
                s["name"] for s in job.get("steps", [])
                if s.get("conclusion") == "failure"
            ]
            failed.append({
                "id": job.get("id"),
                "name": job.get("name", "?"),
                "os": _extract_os(job.get("name", "")),
                "url": job.get("html_url", ""),
                "failed_steps": failed_steps,
            })
    return failed


class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        orig_host = urllib.parse.urlparse(req.full_url).netloc
        new_host = urllib.parse.urlparse(newurl).netloc
        if orig_host != new_host:
            new_req.remove_header("Authorization")
        return new_req


def _get_job_logs(token: str, repo: str, job_id: int) -> str:
    try:
        url = f"{_GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"token {token}")
        req.add_header("Accept", "application/vnd.github+json")
        opener = urllib.request.build_opener(_NoAuthRedirectHandler)
        with opener.open(req, timeout=60) as resp:
            raw = resp.read().decode(errors="replace")
            if len(raw) > 5000:
                return f"...[truncated, showing last 5000 chars]\n{raw[-5000:]}"
            return raw
    except Exception as e:
        return f"Failed to download logs: {e}"


def _extract_os(job_name: str) -> str:
    for os_name in ("ubuntu", "windows", "macos"):
        if os_name in job_name.lower():
            return os_name
    return "unknown"


def _emit_progress(ctx: ToolContext, text: str):
    ctx.pending_events.append({
        "type": "progress",
        "text": text,
        "ts": utc_now_iso(),
    })

def _run_ci_tests(ctx: ToolContext, wait: bool = True, timeout_minutes: int = 15) -> str:
    try:
        token, repo = _get_github_config()
    except ValueError as e:
        return f"⚠️ CI_UNAVAILABLE: {e}"

    repo_dir = str(ctx.repo_dir)
    branch = _get_current_branch(repo_dir)
    sha = _get_current_sha(repo_dir)

    if branch == "HEAD":
        return "⚠️ CI_BRANCH_INVALID: detached HEAD state. Check out a branch first (e.g. `git checkout ouroboros`)."

    try:
        origin_url = run_cmd(["git", "remote", "get-url", "origin"], cwd=repo_dir).strip()
        m = re.search(r"github\.com[/:](.+)", origin_url)
        if m:
            origin_slug = m.group(1).strip().rstrip("/")
            if origin_slug.endswith(".git"):
                origin_slug = origin_slug[:-4]
            if origin_slug.lower() != repo.lower():
                return (
                    f"⚠️ CI_REMOTE_MISMATCH: git origin points to '{origin_slug}' "
                    f"but GITHUB_REPO is '{repo}'. Fix in Settings or update git remote."
                )
        elif "github.com" not in origin_url:
            return (
                f"⚠️ CI_REMOTE_MISMATCH: git origin '{origin_url}' is not a GitHub remote. "
                f"GITHUB_REPO is '{repo}'. CI dispatch requires GitHub."
            )
    except Exception:
        pass  # No origin configured — push will fail naturally

    _emit_progress(ctx, f"📤 Pushing {branch} to origin...")
    ok, push_msg = _push_branch(repo_dir, branch)
    if not ok:
        return f"⚠️ CI_PUSH_FAILED: {push_msg}"

    workflow_id = _find_workflow_id(token, repo)
    if not workflow_id:
        return (
            "⚠️ CI_WORKFLOW_NOT_FOUND: No ci.yml workflow found in the repo. "
            "Push the workflow file first, then retry."
        )

    started_after = utc_now_iso().replace("+00:00", "Z")
    _emit_progress(ctx, f"🚀 Triggering full CI matrix on {branch} ({sha[:8]})...")
    ok, trigger_msg = _trigger_workflow(token, repo, workflow_id, branch)
    if not ok:
        return f"⚠️ CI_TRIGGER_FAILED: {trigger_msg}"

    if not wait:
        return (
            f"✅ CI triggered on {branch} ({sha[:8]}). "
            f"Check results at: https://github.com/{repo}/actions"
        )

    _emit_progress(ctx, "⏳ Waiting for CI results (full 3-OS matrix)...")
    timeout_sec = min(max(timeout_minutes, 1), 30) * 60
    result = _poll_workflow_run(token, repo, branch, sha, started_after, ctx, timeout_sec,
                                workflow_id=workflow_id)

    if result["status"] == "timeout":
        return (
            f"⏰ CI_TIMEOUT: CI did not complete within {timeout_minutes} minutes. "
            f"Check manually: https://github.com/{repo}/actions"
        )

    conclusion = result["conclusion"]
    url = result["url"]
    run_id = result["run_id"]

    if conclusion == "success":
        return (
            f"✅ CI PASSED on all 3 platforms (Ubuntu, Windows, macOS).\n"
            f"Branch: {branch} | SHA: {sha[:8]}\n"
            f"Details: {url}"
        )

    lines = [
        f"❌ CI FAILED (conclusion: {conclusion})",
        f"Branch: {branch} | SHA: {sha[:8]}",
        f"Details: {url}",
        "",
    ]

    if run_id:
        failed_jobs = _get_failed_jobs(token, repo, run_id)
        if failed_jobs:
            lines.append(f"**Failed jobs ({len(failed_jobs)}):**")
            for job in failed_jobs:
                lines.append(f"\n### {job['name']} ({job['os']})")
                if job["failed_steps"]:
                    lines.append(f"Failed steps: {', '.join(job['failed_steps'])}")
                lines.append(f"URL: {job['url']}")

                if job.get("id"):
                    log_text = _get_job_logs(token, repo, job["id"])
                    if log_text:
                        lines.append(f"\n**Logs (tail):**\n```\n{log_text}\n```")
        else:
            lines.append("No failed job details available.")

    return "\n".join(lines)

def get_tools() -> List[ToolEntry]:
    return [ToolEntry(
        name="run_ci_tests",
        schema={
            "name": "run_ci_tests",
            "description": (
                "Push current branch to GitHub and trigger the full CI matrix "
                "(Ubuntu + Windows + macOS tests). Requires GITHUB_TOKEN and GITHUB_REPO "
                "in Settings. Use to verify cross-platform compatibility before releases "
                "or after platform-sensitive changes. Returns pass/fail with failure logs."
            ),
            "parameters": {"type": "object", "properties": {
                "wait": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Wait for CI to complete and return results (default: true). "
                        "Set to false to just trigger and return immediately."
                    ),
                },
                "timeout_minutes": {
                    "type": "integer",
                    "default": 15,
                    "description": "Max minutes to wait for CI completion (1-30, default: 15).",
                },
            }, "required": []},
        },
        handler=_run_ci_tests,
    )]
