"""Deterministic bounded repository context for broad review flows."""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal

from ouroboros.runtime_mode_policy import (
    PROTECTED_RUNTIME_PATH_PREFIXES,
    PROTECTED_RUNTIME_PATHS,
)
from ouroboros.tools.review_helpers import (
    _FULL_REPO_BINARY_EXTENSIONS,
    _FULL_REPO_SKIP_DIR_PREFIXES,
    _MAX_FULL_REPO_FILE_BYTES,
    _SENSITIVE_EXTENSIONS,
    _SENSITIVE_NAMES,
    _VENDORED_NAMES,
    _VENDORED_SUFFIXES,
    _is_probably_binary,
    format_prompt_code_block,
    list_git_tracked_paths,
    redact_prompt_secrets,
)
from ouroboros.utils import estimate_tokens

ATLAS_SCHEMA_VERSION = 1
DEFAULT_ATLAS_TARGET_TOTAL_TOKENS = 850_000
DEFAULT_ATLAS_HARD_TOTAL_TOKENS = 920_000
_ATLAS_MANIFEST_RESERVE_TOKENS = 30_000
_ATLAS_HARD_HEADROOM_TOKENS = 5_000

_CANONICAL_CONTEXT_DOCS = frozenset({
    "BIBLE.md",
    "docs/DEVELOPMENT.md",
    "docs/ARCHITECTURE.md",
    "docs/CHECKLISTS.md",
})

_REVIEW_STACK_PATHS = frozenset({
    "ouroboros/tools/review.py",
    "ouroboros/tools/review_context_atlas.py",
    "ouroboros/tools/scope_review.py",
    "ouroboros/tools/parallel_review.py",
    "ouroboros/tools/review_helpers.py",
    "ouroboros/tools/review_revalidation.py",
    "ouroboros/tools/triad_review.py",
    "ouroboros/tools/claude_advisory_review.py",
    "ouroboros/tools/plan_review.py",
    "ouroboros/triad_review.py",
    "ouroboros/review_state.py",
    "ouroboros/review_evidence.py",
    "ouroboros/deep_self_review.py",
})

_FORCE_INCLUDE_PREFIXES = (
    "prompts/",
    "ouroboros/contracts/",
)

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[^'"]+\s+from\s+)?|export\s+[^'"]+\s+from\s+|import\()\s*['"]([^'"]+)['"]"""
)
_ROUTE_RE = re.compile(r"""['"](/(?:api|ws|owner|static|assets)/[^'"\s{}]+)['"]""")


@dataclass(frozen=True)
class ReviewContextAtlasRequest:
    repo_dir: pathlib.Path
    anchors: tuple[str, ...] = ()
    already_included: frozenset[str] = field(default_factory=frozenset)
    tracked_paths: tuple[str, ...] = ()
    fixed_prompt_tokens: int = 0
    target_total_tokens: int = DEFAULT_ATLAS_TARGET_TOTAL_TOKENS
    hard_total_tokens: int = DEFAULT_ATLAS_HARD_TOTAL_TOKENS
    include_tests: bool = False
    title: str = "Generated Scope Atlas"
    drive_root: pathlib.Path | None = None


@dataclass(frozen=True)
class AtlasFileRecord:
    rel_path: str
    disposition: str
    reason: str
    token_count: int = 0
    score: float = 0.0
    sha256: str = ""
    size_bytes: int = 0
    language: str = ""
    symbols: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    symbol_count: int = 0
    import_count: int = 0
    js_import_count: int = 0
    route_count: int = 0


@dataclass(frozen=True)
class ReviewContextAtlasPack:
    text: str
    manifest: dict
    selected: tuple[AtlasFileRecord, ...]
    omitted: tuple[AtlasFileRecord, ...]
    token_count: int
    status: Literal["ok", "under_target", "budget_constrained", "budget_exceeded"]


@dataclass
class _FileFacts:
    rel_path: str
    size_bytes: int = 0
    sha256: str = ""
    language: str = ""
    content: str = ""
    note: str = ""
    token_count: int = 0
    disposition: str = "manifest_only"
    reason: str = ""
    score: float = 0.0
    required: bool = False
    symbols: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    js_imports: tuple[str, ...] = ()
    routes: tuple[str, ...] = ()
    symbol_count: int = 0
    import_count: int = 0
    js_import_count: int = 0
    route_count: int = 0
    reasons: list[str] = field(default_factory=list)


def compile_review_context_atlas(req: ReviewContextAtlasRequest) -> ReviewContextAtlasPack:
    """Build a bounded deterministic repo context pack plus coverage manifest."""
    repo_dir = pathlib.Path(req.repo_dir)
    anchors = frozenset(_normalize_path(path) for path in req.anchors if _normalize_path(path))
    already_included = frozenset(
        _normalize_path(path) for path in req.already_included if _normalize_path(path)
    )
    manifest_reserve_tokens = min(
        _ATLAS_MANIFEST_RESERVE_TOKENS,
        max(1_000, int(req.target_total_tokens) // 10),
    )
    target_context_tokens = max(
        0,
        int(req.target_total_tokens)
        - max(0, int(req.fixed_prompt_tokens))
        - manifest_reserve_tokens,
    )
    hard_context_tokens = max(
        0,
        int(req.hard_total_tokens)
        - max(0, int(req.fixed_prompt_tokens))
        - _ATLAS_HARD_HEADROOM_TOKENS,
    )

    if req.tracked_paths:
        tracked_paths = [_normalize_path(path) for path in req.tracked_paths]
        inventory_summary = {}
        inventory_by_path: dict[str, Any] = {}
    else:
        try:
            from ouroboros.code_intelligence import build_code_inventory

            inventory = build_code_inventory(repo_dir, drive_root=req.drive_root, persist=True)
            tracked_paths = [_normalize_path(file.path) for file in inventory.files]
            inventory_by_path = {file.path: file for file in inventory.files}
            inventory_summary = {
                "schema_version": inventory.schema_version,
                "git_head": inventory.git_head,
                "file_count": len(inventory.files),
                "coverage": inventory.coverage,
            }
        except Exception as exc:
            try:
                tracked_paths = [_normalize_path(path) for path in list_git_tracked_paths(repo_dir)]
                inventory_summary = {"fallback": "git_ls_files"}
                inventory_by_path = {}
            except Exception as git_exc:
                raise RuntimeError(f"git tracked path inventory unavailable: {exc}; fallback failed: {git_exc}") from git_exc

    facts_by_path = {
        rel: _build_file_facts(
            repo_dir,
            rel,
            anchors,
            already_included,
            req.include_tests,
            inventory_fact=inventory_by_path.get(rel),
        )
        for rel in tracked_paths
        if rel
    }
    _score_relationships(facts_by_path, anchors)

    selected_paths: list[str] = []
    used_tokens = 0
    mandatory_overflow = False

    candidates = [
        facts
        for facts in facts_by_path.values()
        if facts.disposition == "manifest_only" and facts.content
    ]
    candidates.sort(
        key=lambda item: (
            not item.required,
            -item.score,
            item.token_count,
            item.rel_path,
        )
    )

    for facts in candidates:
        limit = hard_context_tokens if facts.required else target_context_tokens
        if used_tokens + facts.token_count <= limit:
            facts.disposition = "full"
            facts.reason = ", ".join(facts.reasons) or "selected"
            selected_paths.append(facts.rel_path)
            used_tokens += facts.token_count
            continue
        if facts.required:
            facts.disposition = "budget_omitted"
            facts.reason = "required file did not fit atlas hard budget"
            mandatory_overflow = True
        else:
            facts.disposition = "manifest_only"
            facts.reason = "not selected within atlas target budget"

    text = _render_atlas_text(req, facts_by_path, selected_paths, status_hint="")
    token_count = estimate_tokens(text)

    if token_count > hard_context_tokens:
        removable = [path for path in reversed(selected_paths) if not facts_by_path[path].required]
        for path in removable:
            facts = facts_by_path[path]
            facts.disposition = "manifest_only"
            facts.reason = "removed to keep atlas below hard budget"
            selected_paths.remove(path)
            text = _render_atlas_text(req, facts_by_path, selected_paths, status_hint="")
            token_count = estimate_tokens(text)
            if token_count <= hard_context_tokens:
                break

    target_text_tokens = max(0, int(req.target_total_tokens) - max(0, int(req.fixed_prompt_tokens)))
    if token_count > target_text_tokens:
        removable = [path for path in reversed(selected_paths) if not facts_by_path[path].required]
        for path in removable:
            facts = facts_by_path[path]
            facts.disposition = "manifest_only"
            facts.reason = "removed to keep assembled prompt near atlas target"
            selected_paths.remove(path)
            text = _render_atlas_text(req, facts_by_path, selected_paths, status_hint="")
            token_count = estimate_tokens(text)
            if token_count <= target_text_tokens:
                break

    if mandatory_overflow or token_count > hard_context_tokens:
        status: Literal["ok", "under_target", "budget_constrained", "budget_exceeded"] = "budget_exceeded"
    elif any(facts.disposition == "manifest_only" and facts.content for facts in facts_by_path.values()):
        status = "budget_constrained"
    elif int(req.fixed_prompt_tokens) + token_count < int(req.target_total_tokens):
        status = "under_target"
    else:
        status = "ok"

    text = _render_atlas_text(req, facts_by_path, selected_paths, status_hint=status)
    token_count = estimate_tokens(text)
    manifest = _build_manifest(req, facts_by_path, selected_paths, token_count, status)
    manifest["code_inventory"] = inventory_summary
    selected = tuple(_record_for(facts_by_path[path]) for path in selected_paths)
    omitted = tuple(
        _record_for(facts)
        for facts in facts_by_path.values()
        if facts.rel_path not in selected_paths
    )
    return ReviewContextAtlasPack(
        text=text,
        manifest=manifest,
        selected=selected,
        omitted=omitted,
        token_count=token_count,
        status=status,
    )


def _build_file_facts(
    repo_dir: pathlib.Path,
    rel: str,
    anchors: frozenset[str],
    already_included: frozenset[str],
    include_tests: bool,
    inventory_fact: Any = None,
) -> _FileFacts:
    facts = _FileFacts(rel_path=rel, language=pathlib.PurePosixPath(rel).suffix.lstrip("."))
    if inventory_fact is not None:
        facts.size_bytes = int(getattr(inventory_fact, "size", 0) or 0)
        digest = str(getattr(inventory_fact, "sha256", "") or "")
        facts.sha256 = digest[:16]
        facts.language = str(getattr(inventory_fact, "language", "") or facts.language)
    path = repo_dir / rel
    if rel in already_included:
        facts.disposition = "already_included"
        facts.reason = "included in fixed prompt context"
    force_include = _is_force_include(rel)

    try:
        resolved = path.resolve()
        resolved.relative_to(repo_dir.resolve())
    except (OSError, ValueError):
        facts.disposition = "path_escape"
        facts.reason = "path escapes repository root"
        return facts

    if not path.is_file():
        if facts.disposition != "already_included":
            facts.disposition = "missing"
            facts.reason = "tracked path is not a regular file"
        return facts

    try:
        raw = path.read_bytes()
        facts.size_bytes = len(raw)
        facts.sha256 = hashlib.sha256(raw).hexdigest()[:16]
    except OSError as exc:
        facts.disposition = "read_error"
        facts.reason = f"read error: {exc}"
        return facts

    if facts.disposition == "already_included":
        return facts

    suffix = pathlib.PurePosixPath(rel).suffix.lower()
    fname = pathlib.PurePosixPath(rel).name.lower()
    if rel.startswith("tests/") and not include_tests and not force_include:
        facts.disposition = "excluded_test"
        facts.reason = "wider tests excluded by atlas policy"
        return facts
    if _skip_by_dir(rel) and not force_include and not (include_tests and rel.startswith("tests/")):
        facts.disposition = "excluded_dir"
        facts.reason = "excluded non-agent-logic directory"
        return facts
    if fname in _SENSITIVE_NAMES or suffix in _SENSITIVE_EXTENSIONS:
        facts.disposition = "sensitive"
        facts.reason = "sensitive filename or extension"
        facts.size_bytes = 0
        facts.sha256 = ""
        return facts
    if suffix in _FULL_REPO_BINARY_EXTENSIONS:
        facts.disposition = "binary_media"
        facts.reason = "binary/media extension"
        return facts
    if fname in _VENDORED_NAMES or any(fname.endswith(suffix_) for suffix_ in _VENDORED_SUFFIXES):
        facts.disposition = "vendored_minified"
        facts.reason = "vendored or minified file"
        return facts
    if facts.size_bytes > _MAX_FULL_REPO_FILE_BYTES:
        facts.disposition = "oversized"
        facts.reason = f">{_MAX_FULL_REPO_FILE_BYTES // 1024}KB"
        return facts
    if _is_probably_binary(path):
        facts.disposition = "binary_media"
        facts.reason = "binary content"
        return facts

    try:
        content = raw.decode("utf-8", errors="replace")
    except Exception as exc:
        facts.disposition = "read_error"
        facts.reason = f"text decode error: {exc}"
        return facts

    content, redacted = redact_prompt_secrets(content)
    facts.content = content
    facts.note = "*(secret-like content redacted)*\n" if redacted else ""
    if inventory_fact is not None and str(getattr(inventory_fact, "language", "") or "") == "python":
        inventory_symbols = list(getattr(inventory_fact, "symbols", []) or [])
        inventory_imports = list(getattr(inventory_fact, "imports", []) or [])
        facts.symbols = tuple(str(getattr(symbol, "name", "") or "") for symbol in inventory_symbols[:16] if str(getattr(symbol, "name", "") or ""))
        facts.imports = tuple(str(item) for item in inventory_imports[:24] if str(item))
        facts.symbol_count = len({str(getattr(symbol, "name", "") or "") for symbol in inventory_symbols if str(getattr(symbol, "name", "") or "")})
        facts.import_count = len(set(str(item) for item in inventory_imports if str(item)))
    else:
        facts.symbols, facts.imports, facts.symbol_count, facts.import_count = _extract_python_facts(rel, content)
    facts.js_imports, facts.js_import_count = _extract_js_imports(rel, content)
    inventory_routes = list(getattr(inventory_fact, "routes", []) or []) if inventory_fact is not None else []
    routes = sorted(set(str(route) for route in inventory_routes if str(route))) or sorted(set(_ROUTE_RE.findall(content)))
    facts.routes = tuple(routes[:12])
    facts.route_count = len(routes)
    facts.token_count = estimate_tokens(_render_file_content(facts))

    if force_include:
        facts.required = True
        facts.score += 10_000
        facts.reasons.append("protected_or_review_surface")
    if rel in anchors:
        facts.required = True
        facts.score += 9_000
        facts.reasons.append("anchor")
    if rel in _CANONICAL_CONTEXT_DOCS:
        facts.required = True
        facts.score += 8_000
        facts.reasons.append("canonical_context_doc")
    if rel.startswith("ouroboros/"):
        facts.score += 200
    if rel.startswith("web/"):
        facts.score += 120
    if rel.startswith("docs/"):
        facts.score += 80
    facts.score += min(len(facts.imports) + len(facts.js_imports), 30) * 10
    facts.reason = "eligible text file"
    return facts


def _score_relationships(facts_by_path: dict[str, _FileFacts], anchors: frozenset[str]) -> None:
    if not anchors:
        return
    module_to_path = {}
    for path, facts in facts_by_path.items():
        module = _python_module_name(path)
        if module:
            module_to_path[module] = path

    anchor_modules = {
        module
        for path in anchors
        for module in (_python_module_name(path),)
        if module
    }
    anchor_dirs = {str(pathlib.PurePosixPath(path).parent) for path in anchors}
    anchor_names = {pathlib.PurePosixPath(path).name for path in anchors}

    for facts in facts_by_path.values():
        if facts.disposition != "manifest_only" or not facts.content:
            continue
        parent = str(pathlib.PurePosixPath(facts.rel_path).parent)
        if parent in anchor_dirs:
            facts.score += 600
            facts.reasons.append("same_directory_as_anchor")
        imported_paths = {
            module_to_path.get(imported)
            for imported in facts.imports
            if module_to_path.get(imported)
        }
        if imported_paths & anchors:
            facts.score += 1_200
            facts.reasons.append("imports_anchor")
        if set(facts.imports) & anchor_modules:
            facts.score += 1_000
            facts.reasons.append("imports_anchor_module")
        if any(name in facts.content for name in anchor_names):
            facts.score += 300
            facts.reasons.append("mentions_anchor_path")

    for anchor in anchors:
        anchor_facts = facts_by_path.get(anchor)
        if not anchor_facts:
            continue
        for imported in anchor_facts.imports:
            imported_path = module_to_path.get(imported)
            if imported_path and imported_path in facts_by_path:
                facts = facts_by_path[imported_path]
                if facts.disposition == "manifest_only" and facts.content:
                    facts.score += 1_200
                    facts.reasons.append("imported_by_anchor")
        for js_path in anchor_facts.js_imports:
            if js_path in facts_by_path:
                facts = facts_by_path[js_path]
                if facts.disposition == "manifest_only" and facts.content:
                    facts.score += 1_200
                    facts.reasons.append("js_imported_by_anchor")


def _extract_python_facts(
    rel: str,
    content: str,
) -> tuple[tuple[str, ...], tuple[str, ...], int, int]:
    if not rel.endswith(".py"):
        return (), (), 0, 0
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return (), (), 0, 0
    symbols: list[str] = []
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    symbols.append(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id.isupper():
            symbols.append(node.target.id)
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from_module(rel, node)
            if module:
                imports.append(module)
    unique_symbols = sorted(set(symbols))
    unique_imports = sorted(set(imports))
    return tuple(unique_symbols[:16]), tuple(unique_imports[:24]), len(unique_symbols), len(unique_imports)


def _extract_js_imports(rel: str, content: str) -> tuple[tuple[str, ...], int]:
    if not rel.endswith((".js", ".mjs", ".ts", ".tsx", ".jsx")):
        return (), 0
    parent = pathlib.PurePosixPath(rel).parent
    found: set[str] = set()
    for spec in _JS_IMPORT_RE.findall(content):
        if not spec.startswith("."):
            continue
        base = (parent / spec).as_posix()
        candidates = [base]
        if not pathlib.PurePosixPath(base).suffix:
            candidates.extend([base + ".js", base + ".mjs", base + "/index.js"])
        found.update(candidates)
    sorted_found = sorted(found)
    return tuple(sorted_found[:24]), len(sorted_found)


def _python_module_name(rel: str) -> str:
    if not rel.endswith(".py"):
        return ""
    path = rel[:-3]
    if path.endswith("/__init__"):
        path = path[: -len("/__init__")]
    return path.replace("/", ".")


def _resolve_import_from_module(rel: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    current = _python_module_name(rel)
    package_parts = current.split(".")[:-1] if current else []
    base = package_parts[: max(0, len(package_parts) - (node.level - 1))]
    if node.module:
        return ".".join([*base, *node.module.split(".")])
    if node.names:
        return ".".join([*base, node.names[0].name])
    return ".".join(base)


def _is_force_include(rel: str) -> bool:
    return (
        rel in PROTECTED_RUNTIME_PATHS
        or rel in _REVIEW_STACK_PATHS
        or any(rel.startswith(prefix) for prefix in PROTECTED_RUNTIME_PATH_PREFIXES)
        or any(rel.startswith(prefix) for prefix in _FORCE_INCLUDE_PREFIXES)
    )


def _skip_by_dir(rel: str) -> bool:
    return rel.startswith(_FULL_REPO_SKIP_DIR_PREFIXES)


def _render_file_content(facts: _FileFacts) -> str:
    return (
        f"### {facts.rel_path}\n"
        f"sha256={facts.sha256} size={facts.size_bytes}B"
        + (
            f" symbols={', '.join(facts.symbols[:8])}"
            + (f" (+{facts.symbol_count - len(facts.symbols)} more)" if facts.symbol_count > len(facts.symbols) else "")
            if facts.symbols else ""
        )
        + (
            f" imports={', '.join(facts.imports[:8])}"
            + (f" (+{facts.import_count - len(facts.imports)} more)" if facts.import_count > len(facts.imports) else "")
            if facts.imports else ""
        )
        + "\n"
        f"{facts.note}{format_prompt_code_block(facts.content, facts.language)}\n\n"
    )


def _render_atlas_text(
    req: ReviewContextAtlasRequest,
    facts_by_path: dict[str, _FileFacts],
    selected_paths: list[str],
    *,
    status_hint: str,
) -> str:
    counts = Counter(facts.disposition for facts in facts_by_path.values())
    selected = [facts_by_path[path] for path in selected_paths]
    manifest_preview = {
        "schema_version": ATLAS_SCHEMA_VERSION,
        "strategy": "generated_scope_atlas",
        "status": status_hint or "building",
        "fixed_prompt_tokens": int(req.fixed_prompt_tokens),
        "target_total_tokens": int(req.target_total_tokens),
        "hard_total_tokens": int(req.hard_total_tokens),
        "selected_count": len(selected),
        "tracked_count": len(facts_by_path),
        "dispositions": dict(sorted(counts.items())),
        "selected": [_manifest_row(facts) for facts in selected],
        "coverage": [_manifest_row(facts) for facts in facts_by_path.values()],
    }
    parts = [
        f"## {req.title}",
        "",
        "This is a generated, deterministic, bounded repository atlas. It replaces the raw full-repo pack for this review flow. Summaries here are structural facts only; they are not LLM-generated claims.",
        "",
        "### Atlas token accounting",
        "",
        f"- fixed_prompt_tokens: {int(req.fixed_prompt_tokens):,}",
        f"- target_total_tokens: {int(req.target_total_tokens):,}",
        f"- hard_total_tokens: {int(req.hard_total_tokens):,}",
        f"- selected_full_files: {len(selected):,}",
        f"- tracked_files_accounted: {len(facts_by_path):,}",
        "",
        "### Coverage manifest",
        "",
        format_prompt_code_block(json.dumps(manifest_preview, ensure_ascii=False, indent=2), "json"),
        "",
        "### Atlas full file contents",
        "",
    ]
    if selected:
        parts.extend(_render_file_content(facts) for facts in selected)
    else:
        parts.append("(no additional files selected for full atlas context)\n")
    return "\n".join(parts)


def _build_manifest(
    req: ReviewContextAtlasRequest,
    facts_by_path: dict[str, _FileFacts],
    selected_paths: list[str],
    token_count: int,
    status: str,
) -> dict:
    counts = Counter(facts.disposition for facts in facts_by_path.values())
    return {
        "schema_version": ATLAS_SCHEMA_VERSION,
        "strategy": "generated_scope_atlas",
        "status": status,
        "fixed_prompt_tokens": int(req.fixed_prompt_tokens),
        "atlas_tokens": int(token_count),
        "estimated_total_tokens": int(req.fixed_prompt_tokens) + int(token_count),
        "target_total_tokens": int(req.target_total_tokens),
        "hard_total_tokens": int(req.hard_total_tokens),
        "selected_count": len(selected_paths),
        "tracked_count": len(facts_by_path),
        "dispositions": dict(sorted(counts.items())),
        "selected": [_manifest_row(facts_by_path[path]) for path in selected_paths],
        "coverage": [_manifest_row(facts) for facts in facts_by_path.values()],
    }


def _manifest_row(facts: _FileFacts) -> dict:
    row = {
        "path": facts.rel_path,
        "disposition": facts.disposition,
        "reason": facts.reason or ", ".join(facts.reasons),
        "sha256": facts.sha256,
        "size": facts.size_bytes,
        "tokens": facts.token_count,
    }
    if facts.symbols:
        row["symbols"] = list(facts.symbols[:12])
        row["symbols_total"] = facts.symbol_count
    if facts.imports:
        row["imports"] = list(facts.imports[:12])
        row["imports_total"] = facts.import_count
    if facts.js_imports:
        row["js_imports"] = list(facts.js_imports[:12])
        row["js_imports_total"] = facts.js_import_count
    if facts.routes:
        row["routes"] = list(facts.routes[:12])
        row["routes_total"] = facts.route_count
    return row


def _record_for(facts: _FileFacts) -> AtlasFileRecord:
    return AtlasFileRecord(
        rel_path=facts.rel_path,
        disposition=facts.disposition,
        reason=facts.reason or ", ".join(facts.reasons),
        token_count=facts.token_count,
        score=facts.score,
        sha256=facts.sha256,
        size_bytes=facts.size_bytes,
        language=facts.language,
        symbols=facts.symbols,
        imports=facts.imports,
        routes=facts.routes,
        symbol_count=facts.symbol_count,
        import_count=facts.import_count,
        js_import_count=facts.js_import_count,
        route_count=facts.route_count,
    )


def _normalize_path(path: str) -> str:
    cleaned = str(path or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return pathlib.PurePosixPath(cleaned).as_posix() if cleaned else ""
