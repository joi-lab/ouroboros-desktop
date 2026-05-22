"""Publish reviewed local skills to OuroborosHub via GitHub PR."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import urllib.parse
from typing import Any, Dict, List, Tuple

from ouroboros.config import (
    SKILL_SOURCE_CLAWHUB,
    SKILL_SOURCE_EXTERNAL,
    SKILL_SOURCE_NATIVE,
    SKILL_SOURCE_OUROBOROSHUB,
    SKILL_SOURCE_SELF_AUTHORED,
    SKILL_SOURCE_USER_REPO,
    get_ouroboroshub_catalog_url,
    get_light_model,
)
from ouroboros.llm import LLMClient
from ouroboros.skill_loader import (
    SkillPayloadUnreadable,
    _iter_payload_files,
    _sanitize_skill_name,
    compute_content_hash,
    find_skill,
)
from ouroboros.skill_review_status import STATUS_CLEAN, normalize_skill_review_status
from ouroboros.contracts.skill_payload_policy import SKILL_PAYLOAD_CONTROL_FILENAMES
from ouroboros.tools.github import _gh_cmd, github_token_from_env_or_settings
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import contains_real_secret_value, read_json_dict, utc_now_iso

_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
_BRANCH_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _warn(message: str) -> str:
    return f"⚠️ SUBMIT_BLOCKED: {message}"


def _parse_hub_destination(catalog_url: str) -> Tuple[str, str, str]:
    parsed = urllib.parse.urlparse(str(catalog_url or "").strip())
    if parsed.netloc != "raw.githubusercontent.com":
        raise ValueError(f"catalog URL must point to raw.githubusercontent.com (got: {parsed.netloc or 'empty'})")
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 4 or parts[-1] != "catalog.json":
        raise ValueError("catalog URL path must look like /<owner>/<repo>/<branch>/catalog.json")
    return parts[0], parts[1], "/".join(parts[2:-1])


def _gh_json(ctx: ToolContext, args: List[str], *, timeout: int = 30, input_data: str | None = None) -> Dict[str, Any]:
    raw = _gh_cmd(args, ctx, timeout=timeout, input_data=input_data)
    if raw.startswith("⚠️"):
        raise RuntimeError(raw)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"⚠️ GH_ERROR: failed to parse JSON response: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("⚠️ GH_ERROR: expected JSON object from gh")
    return data


def _skill_payload_files(skill_dir: pathlib.Path, manifest: Any) -> List[Dict[str, Any]]:
    files = []
    total = 0
    for file_path in _iter_payload_files(
        skill_dir,
        manifest_entry=getattr(manifest, "entry", "") or "",
        manifest_scripts=getattr(manifest, "scripts", []) or [],
    ):
        rel = file_path.relative_to(skill_dir).as_posix()
        if pathlib.PurePosixPath(rel).name.lower() in SKILL_PAYLOAD_CONTROL_FILENAMES:
            continue
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            raise ValueError(f"cannot stat skill payload file {rel}: {exc}") from exc
        if total + size > _MAX_PAYLOAD_BYTES:
            raise ValueError("skill payload too large for OuroborosHub (limit: 5 MB)")
        data = file_path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        if text:
            has_secret, _matches = contains_real_secret_value(text)
            if has_secret:
                raise ValueError(f"secret value found in {rel}; remove it before publishing")
        total += len(data)
        files.append({
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "content_b64": base64.b64encode(data).decode("ascii"),
        })
    return files


def _catalog_entry(skill: str, manifest: Any, payload_files: List[Dict[str, Any]]) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "slug": skill,
        "name": getattr(manifest, "name", "") or skill,
        "description": getattr(manifest, "description", "") or "",
        "version": getattr(manifest, "version", "") or "",
        "type": getattr(manifest, "type", "") or "",
        "files": [
            {"path": f["path"], "sha256": f["sha256"], "size": f["size"]}
            for f in payload_files
        ],
    }
    raw_extra = getattr(manifest, "raw_extra", {}) or {}
    install_specs = raw_extra.get("install_specs") or raw_extra.get("install") or raw_extra.get("dependencies")
    if install_specs:
        entry["install_specs"] = install_specs
    when_to_use = getattr(manifest, "when_to_use", "") or ""
    if when_to_use:
        entry["when_to_use"] = when_to_use
    return entry


def _update_catalog(catalog: Dict[str, Any], entry: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    skills = catalog.get("skills")
    if not isinstance(skills, list):
        raise ValueError("catalog.skills must be a list")
    slug = str(entry.get("slug") or "")
    existing_index = next(
        (idx for idx, item in enumerate(skills) if isinstance(item, dict) and item.get("slug") == slug),
        None,
    )
    if existing_index is None:
        mode = "add"
        skills.append(entry)
    else:
        existing = skills[existing_index]
        if str(existing.get("version") or "") == str(entry.get("version") or ""):
            raise RuntimeError(
                f"⚠️ SUBMIT_NOOP: {slug} v{entry.get('version')} already exists in the catalog. "
                "Bump the skill version, re-review, then retry."
            )
        mode = "update"
        skills[existing_index] = entry
    skills.sort(key=lambda item: str(item.get("slug") or "") if isinstance(item, dict) else "")
    catalog["skills"] = skills
    return mode, catalog


_PROVENANCE_SLUG_MAX = 128


def _provenance_hint(skill_dir: pathlib.Path, source: str) -> str:
    """Return safe marketplace provenance Markdown, or empty for non-managed skills."""
    if source == SKILL_SOURCE_OUROBOROSHUB:
        marker = skill_dir / ".ouroboroshub.json"
        label = "OuroborosHub"
        slug_key = "slug"
    elif source == SKILL_SOURCE_CLAWHUB:
        marker = skill_dir / ".clawhub.json"
        label = "ClawHub"
        slug_key = "clawhub_slug"
    else:
        return ""
    data = read_json_dict(marker)
    if data is None:
        return ""
    original = str(data.get(slug_key) or data.get("slug") or "").strip()
    if not original:
        return ""
    has_secret, _matches = contains_real_secret_value(original)
    if has_secret:
        # Never leak suspicious sidecar slugs into a public PR body.
        return ""
    # Prevent inline-code breakout and fake Markdown headings.
    safe = "".join(ch for ch in original if 0x20 <= ord(ch) != 0x7f)
    safe = safe.replace("`", "").strip()
    if not safe:
        return ""
    if len(safe) > _PROVENANCE_SLUG_MAX:
        safe = safe[:_PROVENANCE_SLUG_MAX] + "…"
    return (
        f"## Provenance\n"
        f"Locally installed from {label} as `{safe}`. "
        f"This PR submits a locally adapted version.\n\n"
    )


def _validate_local_skill(ctx: ToolContext, skill: str):
    safe = _sanitize_skill_name(skill)
    if not safe or safe == "_unnamed":
        raise ValueError("skill name is required")
    if not github_token_from_env_or_settings():
        raise ValueError("GITHUB_TOKEN missing in Settings -> Secrets")
    loaded = find_skill(pathlib.Path(ctx.drive_root), safe)
    if loaded is None:
        raise ValueError(f"skill not found: {safe}")
    allowed_sources = {
        SKILL_SOURCE_EXTERNAL,
        SKILL_SOURCE_SELF_AUTHORED,
        SKILL_SOURCE_USER_REPO,
        SKILL_SOURCE_OUROBOROSHUB,
        SKILL_SOURCE_CLAWHUB,
    }
    if loaded.source == SKILL_SOURCE_NATIVE and not (loaded.skill_dir / ".seed-origin").is_file():
        allowed_sources.add(SKILL_SOURCE_NATIVE)
    if loaded.source not in allowed_sources:
        raise ValueError(f"skill source {loaded.source!r} cannot be submitted to OuroborosHub")
    if loaded.load_error:
        raise ValueError(f"skill has a load error: {loaded.load_error}")
    if normalize_skill_review_status(loaded.review.status) != STATUS_CLEAN:
        raise ValueError("skill must have a fresh clean review before publishing")
    current_hash = compute_content_hash(
        loaded.skill_dir,
        manifest_entry=loaded.manifest.entry,
        manifest_scripts=loaded.manifest.scripts,
    )
    if loaded.review.is_stale_for(current_hash):
        raise ValueError("review is stale; re-review the skill first")
    if not str(loaded.manifest.version or "").strip():
        raise ValueError("skill manifest version is required")
    return safe, loaded


def _ensure_user_fork(ctx: ToolContext, owner: str, repo: str, base_branch: str) -> str:
    login = _gh_cmd(["api", "/user", "--jq", ".login"], ctx).strip()
    if login.startswith("⚠️"):
        raise RuntimeError(login)
    if not login:
        raise RuntimeError("⚠️ GH_ERROR: could not determine GitHub login")
    view = _gh_cmd(["repo", "view", f"{login}/{repo}", "--json", "name"], ctx)
    if view.startswith("⚠️"):
        fork = _gh_cmd(["repo", "fork", f"{owner}/{repo}", "--clone=false"], ctx, timeout=60)
        if fork.startswith("⚠️"):
            raise RuntimeError(fork)
    merge = _gh_cmd(
        ["api", "-X", "POST", f"/repos/{login}/{repo}/merge-upstream", "-f", f"branch={base_branch}"],
        ctx,
        timeout=45,
    )
    if merge.startswith("⚠️"):
        ctx.emit_progress_fn(f"OuroborosHub fork sync warning: {merge}")
    return login


def _fetch_upstream_catalog(ctx: ToolContext, owner: str, repo: str, base_branch: str) -> Tuple[Dict[str, Any], str]:
    ref = _gh_json(ctx, ["api", f"/repos/{owner}/{repo}/git/refs/heads/{base_branch}"])
    base_sha = str((ref.get("object") or {}).get("sha") or "")
    if not base_sha:
        raise RuntimeError("⚠️ GH_ERROR: upstream base branch SHA unavailable")
    content = _gh_json(ctx, ["api", f"/repos/{owner}/{repo}/contents/catalog.json?ref={base_branch}"])
    raw_content = str(content.get("content") or "")
    try:
        catalog_bytes = base64.b64decode(raw_content)
        catalog = json.loads(catalog_bytes.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"⚠️ GH_ERROR: failed to decode upstream catalog.json: {exc}") from exc
    if not isinstance(catalog, dict):
        raise RuntimeError("⚠️ GH_ERROR: catalog.json root must be an object")
    return catalog, base_sha


def _ensure_branch(ctx: ToolContext, login: str, repo: str, branch: str, base_sha: str) -> str:
    existing = _gh_cmd(["api", f"/repos/{login}/{repo}/git/ref/heads/{branch}"], ctx)
    if not existing.startswith("⚠️"):
        raise RuntimeError(f"⚠️ SUBMIT_BLOCKED: branch {branch!r} already exists; delete it on GitHub or bump version, then retry.")
    created = _gh_cmd(
        ["api", "-X", "POST", f"/repos/{login}/{repo}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={base_sha}"],
        ctx,
    )
    if created.startswith("⚠️"):
        raise RuntimeError(created)
    try:
        data = json.loads(created)
        sha = str((data.get("object") or {}).get("sha") or "")
    except Exception:
        sha = ""
    return sha or base_sha


def _commit_payload(
    ctx: ToolContext,
    login: str,
    repo: str,
    branch: str,
    base_sha: str,
    headline: str,
    additions: List[Dict[str, str]],
) -> str:
    query = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
  }
}
""".strip()
    payload = {
        "query": query,
        "variables": {
            "input": {
                "branch": {"repositoryNameWithOwner": f"{login}/{repo}", "branchName": branch},
                "message": {"headline": headline},
                "fileChanges": {"additions": additions},
                "expectedHeadOid": base_sha,
            }
        },
    }
    result = _gh_json(ctx, ["api", "graphql", "--input", "-"], timeout=60, input_data=json.dumps(payload))
    errors = result.get("errors")
    if errors:
        raise RuntimeError(f"⚠️ GH_ERROR: GraphQL commit failed: {str(errors)[:500]}")
    commit = ((result.get("data") or {}).get("createCommitOnBranch") or {}).get("commit") or {}
    return str(commit.get("url") or commit.get("oid") or "")


def _generate_pr_body(
    ctx: ToolContext,
    mode: str,
    skill: str,
    manifest: Any,
    files: List[Dict[str, Any]],
    note: str,
    skill_dir: pathlib.Path,
    source: str = "",
) -> str:
    provenance = _provenance_hint(skill_dir, source)
    fallback = provenance + (
        f"## Summary\n"
        f"- {mode.title()} `{skill}` v{manifest.version} to OuroborosHub.\n"
        f"- Type: `{manifest.type}`; files: {len(files)}.\n\n"
        f"## What This Skill Does\n{manifest.description or 'See SKILL.md.'}\n\n"
        f"## Author Checklist\n- Fresh clean review verified locally.\n- Payload hash matches the reviewed state.\n- No local Ouroboros repo mutation was required.\n"
    )
    if note.strip():
        note_has_secret, _matches = contains_real_secret_value(note)
        if note_has_secret:
            raise ValueError("secret value found in submit note")
        fallback = provenance + f"## Note\n{note.strip()}\n\n" + fallback[len(provenance):]
    skill_md = ""
    skill_md_path = skill_dir / "SKILL.md"
    try:
        # Prefer reviewed SKILL.md when small; fallback stays valid if unavailable.
        if skill_md_path.is_file():
            text = skill_md_path.read_text(encoding="utf-8")
            skill_md = text[:8192]
            skill_md_has_secret, _matches = contains_real_secret_value(skill_md)
            if skill_md_has_secret:
                raise ValueError("secret value found in SKILL.md; remove it before publishing")
    except OSError:
        pass
    if not skill_md:
        return fallback
    try:
        llm = LLMClient()
        model = get_light_model()
        prompt = (
            "Write a concise GitHub pull request body in Markdown for publishing an OuroborosHub skill. "
            "Use sections: Summary, What this skill does, Author checklist. "
            "Do not invent claims. Include the optional author note if provided.\n\n"
            f"Mode: {mode}\nSkill: {skill}\nVersion: {manifest.version}\nType: {manifest.type}\n"
            f"Files: {', '.join(f['path'] for f in files[:50])}\n"
            f"Author note: {note.strip() or '(none)'}\n"
            + (f"Provenance: {provenance.strip()}\n" if provenance else "")
            + f"\nSKILL.md:\n{skill_md}"
        )
        response, usage = llm.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            reasoning_effort="low",
            max_tokens=8192,
            use_local=os.environ.get("USE_LOCAL_LIGHT", "").lower() in ("true", "1"),
        )
        if usage:
            event = {
                "type": "llm_usage",
                "provider": "skill_publish",
                "model": model,
                "usage": usage,
                "cost": float(usage.get("cost") or 0.0),
                "source": "submit_skill_to_hub",
                "ts": utc_now_iso(),
                "category": "task",
            }
            ctx.pending_events.append(event)
        body = str(response.get("content") or "").strip()
        if not body:
            return fallback
        # Do not let LLM prose silently drop marketplace provenance.
        if provenance and "## Provenance" not in body:
            body = provenance + body
        return body
    except Exception as exc:
        ctx.emit_progress_fn(f"PR body LLM fallback: {type(exc).__name__}: {exc}")
        return fallback


def _open_pr(ctx: ToolContext, owner: str, repo: str, base_branch: str, login: str, branch: str, title: str, body: str) -> str:
    raw = _gh_cmd(
        ["pr", "create", "--repo", f"{owner}/{repo}", "--base", base_branch, "--head", f"{login}:{branch}", "--title", title, "--body-file", "-"],
        ctx,
        timeout=60,
        input_data=body,
    )
    if raw.startswith("⚠️"):
        raise RuntimeError(raw)
    return raw.strip()


def _submit_skill_to_hub(
    ctx: ToolContext,
    skill: str,
    note: str = "",
    confirm_public_submission: bool = False,
    permission_statement: str = "",
) -> str:
    try:
        if not confirm_public_submission:
            return _warn("explicit public submission confirmation is required")
        if "publish" not in permission_statement.lower() and "submit" not in permission_statement.lower():
            return _warn("permission_statement must state that the human explicitly asked to publish/submit this skill")
        safe_skill, loaded = _validate_local_skill(ctx, skill)
        owner, repo, base_branch = _parse_hub_destination(get_ouroboroshub_catalog_url())
        login = _ensure_user_fork(ctx, owner, repo, base_branch)
        catalog, base_sha = _fetch_upstream_catalog(ctx, owner, repo, base_branch)
        payload_files = _skill_payload_files(loaded.skill_dir, loaded.manifest)
        final_hash = compute_content_hash(
            loaded.skill_dir,
            manifest_entry=loaded.manifest.entry,
            manifest_scripts=loaded.manifest.scripts,
        )
        if loaded.review.is_stale_for(final_hash):
            return _warn("review became stale while preparing payload; re-review the skill first")
        entry = _catalog_entry(safe_skill, loaded.manifest, payload_files)
        mode, updated_catalog = _update_catalog(catalog, entry)
        branch_version = _BRANCH_SEGMENT_RE.sub("-", str(loaded.manifest.version)).strip("-") or "unknown"
        branch = f"submit/{safe_skill}-v{branch_version}"
        branch_sha = _ensure_branch(ctx, login, repo, branch, base_sha)
        additions = [
            {"path": f"skills/{safe_skill}/{item['path']}", "contents": item["content_b64"]}
            for item in payload_files
        ]
        catalog_bytes = json.dumps(updated_catalog, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        additions.append({"path": "catalog.json", "contents": base64.b64encode(catalog_bytes).decode("ascii")})
        title = f"{mode.title()} skill: {safe_skill} v{loaded.manifest.version}"
        _commit_payload(ctx, login, repo, branch, branch_sha, title, additions)
        body = _generate_pr_body(ctx, mode, safe_skill, loaded.manifest, payload_files, note or "", loaded.skill_dir, loaded.source)
        pr_url = _open_pr(ctx, owner, repo, base_branch, login, branch, title, body)
        return (
            f"✅ PR opened: {pr_url}\n"
            f"Mode: {mode}\n"
            f"Files: {len(payload_files)}\n"
            f"Branch: {login}:{branch}"
        )
    except SkillPayloadUnreadable as exc:
        return _warn(str(exc))
    except ValueError as exc:
        return _warn(str(exc))
    except RuntimeError as exc:
        text = str(exc)
        return text if text.startswith("⚠️") else _warn(text)
    except Exception as exc:
        return f"⚠️ SUBMIT_ERROR: {type(exc).__name__}: {exc}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="submit_skill_to_hub",
            schema={
                "name": "submit_skill_to_hub",
                "description": (
                    "Submit a locally-installed skill to OuroborosHub by opening a Pull Request "
                    "from the user's GitHub fork. Auto-detects Add vs Update based on the upstream "
                    "catalog. Requires GITHUB_TOKEN configured in Settings. Returns the PR URL on success."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string", "description": "Skill name (slug) under data/skills/"},
                        "note": {
                            "type": "string",
                            "default": "",
                            "description": "Optional author note, prepended to the PR body the LLM generates.",
                        },
                        "confirm_public_submission": {
                            "type": "boolean",
                            "description": "Must be true: confirms the human explicitly approved public submission to OuroborosHub.",
                        },
                        "permission_statement": {
                            "type": "string",
                            "description": "Short statement of the human's explicit request to publish/submit this skill.",
                        },
                    },
                    "required": ["skill", "confirm_public_submission", "permission_statement"],
                },
            },
            handler=_submit_skill_to_hub,
            is_code_tool=False,
            timeout_sec=180,
        ),
    ]


__all__ = ["get_tools"]
