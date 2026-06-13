"""Internal deterministic code inventory v1.

No embeddings, no LSP, no SQLite, and no raw source cache. This is a compact
structural projection used by digest/review context builders.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import re
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Dict, List

from ouroboros.utils import atomic_write_json, utc_now_iso


_SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", "node_modules", "dist", "build",
}
_JS_IMPORT_RE = re.compile(r"""(?m)^\s*(?:import\s+.*?\s+from\s+|import\s*\(|require\s*\()\s*['"]([^'"]+)['"]""")
_ROUTE_RE = re.compile(r"""(?i)(?:route|path)\s*[:=]\s*['"]([^'"]+)['"]|@\w+\.route\(['"]([^'"]+)['"]""")
_SENSITIVE_NAME_RE = re.compile(r"(?i)(token|secret|credential|private[_-]?key|api[_-]?key|password|passwd)")
_SENSITIVE_EXTENSIONS = {".json", ".env", ".key", ".pem", ".p12", ".pfx", ".crt", ".cer"}
_MAX_INDEX_FILE_BYTES = 2_000_000


@dataclass
class SymbolFact:
    name: str
    kind: str
    line_start: int
    line_end: int
    signature: str = ""


@dataclass
class FileFact:
    path: str
    sha256: str
    size: int
    language: str
    token_estimate: int
    disposition: str = "indexed"
    syntax_error: str = ""
    symbols: List[SymbolFact] = field(default_factory=list)
    imports: List[str] = field(default_factory=list)
    resolved_import_paths: List[str] = field(default_factory=list)
    routes: List[str] = field(default_factory=list)


@dataclass
class CodeInventory:
    schema_version: int
    repo_root: str
    git_head: str
    created_at: str
    files: List[FileFact]
    coverage: Dict[str, int]

    def to_json(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "repo_root": self.repo_root,
            "git_head": self.git_head,
            "created_at": self.created_at,
            "files": [
                {
                    **asdict(file),
                    "symbols": [asdict(symbol) for symbol in file.symbols],
                }
                for file in self.files
            ],
            "coverage": dict(self.coverage),
        }


def _git_head(repo_root: pathlib.Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def _tracked_files(repo_root: pathlib.Path) -> List[pathlib.Path]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=10,
        )
        if proc.returncode == 0:
            return [repo_root / part.decode("utf-8", errors="replace") for part in proc.stdout.split(b"\0") if part]
    except Exception:
        pass
    paths: List[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SKIP_DIRS]
        for name in sorted(filenames):
            paths.append(pathlib.Path(dirpath) / name)
    return paths


def _language(path: pathlib.Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".go": "go",
        ".md": "markdown",
        ".json": "json",
        ".toml": "toml",
        ".yaml": "yaml",
        ".yml": "yaml",
    }.get(suffix, suffix.lstrip(".") or "text")


def _signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = [arg.arg for arg in node.args.args]
        return f"{node.name}({', '.join(args)})"
    if isinstance(node, ast.ClassDef):
        return f"class {node.name}"
    return ""


def _resolve_relative_import(rel_path: pathlib.PurePosixPath, module: str, level: int) -> str:
    if level <= 0:
        return module
    package_parts = list(rel_path.parent.parts)
    keep = max(0, len(package_parts) - level + 1)
    parts = package_parts[:keep]
    if module:
        parts.extend(str(module).split("."))
    return ".".join(part for part in parts if part)


def _python_facts(text: str, rel_path: pathlib.PurePosixPath) -> tuple[List[SymbolFact], List[str], str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [], [], f"{exc.msg} at line {exc.lineno}"
    symbols: List[SymbolFact] = []
    imports: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.append(SymbolFact(
                name=node.name,
                kind="class" if isinstance(node, ast.ClassDef) else ("async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"),
                line_start=int(getattr(node, "lineno", 0) or 0),
                line_end=int(getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0),
                signature=_signature(node),
            ))
        elif isinstance(node, ast.Assign):
            if all(isinstance(target, ast.Name) and target.id.isupper() for target in node.targets):
                for target in node.targets:
                    symbols.append(SymbolFact(target.id, "constant", int(getattr(node, "lineno", 0) or 0), int(getattr(node, "lineno", 0) or 0)))
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and not node.module:
                imports.extend(
                    _resolve_relative_import(rel_path, alias.name, int(node.level or 0))
                    for alias in node.names
                    if alias.name and alias.name != "*"
                )
            elif node.module or node.level:
                imports.append(_resolve_relative_import(rel_path, node.module or "", int(node.level or 0)))
    symbols.sort(key=lambda item: (item.line_start, item.name))
    return symbols, sorted(set(imports)), ""


def _resolve_python_import(repo_root: pathlib.Path, module: str) -> str:
    rel = pathlib.Path(*str(module or "").split("."))
    for candidate in (repo_root / rel.with_suffix(".py"), repo_root / rel / "__init__.py"):
        if candidate.is_file():
            try:
                return candidate.relative_to(repo_root).as_posix()
            except ValueError:
                return ""
    return ""


def _file_fact(repo_root: pathlib.Path, path: pathlib.Path) -> FileFact:
    try:
        rel = path.relative_to(repo_root).as_posix()
    except ValueError:
        try:
            rel = path.resolve(strict=False).relative_to(repo_root).as_posix()
        except ValueError:
            return FileFact(str(path), "", 0, _language(path), 0, disposition="path_escape")
    if path.is_symlink():
        try:
            path.resolve(strict=False).relative_to(repo_root)
        except ValueError:
            return FileFact(rel, "", 0, _language(path), 0, disposition="path_escape")
    if _is_sensitive_inventory_path(rel):
        return FileFact(rel, "", 0, _language(path), 0, disposition="sensitive")
    try:
        stat_size = path.stat().st_size
    except OSError as exc:
        return FileFact(rel, "", 0, _language(path), 0, disposition=f"read_error:{exc}")
    if stat_size > _MAX_INDEX_FILE_BYTES:
        return FileFact(rel, "", stat_size, _language(path), 0, disposition="oversized")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return FileFact(rel, "", 0, _language(path), 0, disposition=f"read_error:{exc}")
    digest = hashlib.sha256(raw).hexdigest()
    lang = _language(path)
    token_est = max(1, len(raw) // 4)
    if b"\0" in raw[:4096]:
        return FileFact(rel, digest, len(raw), lang, token_est, disposition="binary")
    text = raw.decode("utf-8", errors="replace")
    if lang == "python":
        symbols, imports, syntax_error = _python_facts(text, pathlib.PurePosixPath(rel))
        resolved = [p for p in (_resolve_python_import(repo_root, module) for module in imports) if p]
        return FileFact(rel, digest, len(raw), lang, token_est, syntax_error=syntax_error, symbols=symbols, imports=imports, resolved_import_paths=resolved)
    if lang in {"javascript", "typescript"}:
        imports = sorted(set(_JS_IMPORT_RE.findall(text)))
        routes = sorted({match[0] or match[1] for match in _ROUTE_RE.findall(text) if match[0] or match[1]})
        return FileFact(rel, digest, len(raw), lang, token_est, imports=imports, routes=routes[:50])
    return FileFact(rel, digest, len(raw), lang, token_est)


def _is_sensitive_inventory_path(rel_path: str) -> bool:
    rel = str(rel_path or "").replace("\\", "/")
    name = pathlib.PurePosixPath(rel).name
    lower = name.lower()
    if lower == ".env" or lower.startswith(".env."):
        return True
    suffix = pathlib.PurePosixPath(rel).suffix.lower()
    return suffix in _SENSITIVE_EXTENSIONS and bool(_SENSITIVE_NAME_RE.search(name))


def build_code_inventory(repo_root: pathlib.Path, *, drive_root: pathlib.Path | None = None, persist: bool = True) -> CodeInventory:
    root = pathlib.Path(repo_root).resolve(strict=False)
    files = []
    for path in _tracked_files(root):
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            rel_parts = path.parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        if path.is_file():
            files.append(_file_fact(root, path))
    coverage: Dict[str, int] = {}
    for file in files:
        coverage[file.disposition] = coverage.get(file.disposition, 0) + 1
    inventory = CodeInventory(
        schema_version=1,
        repo_root=str(root),
        git_head=_git_head(root),
        created_at=utc_now_iso(),
        files=files,
        coverage=coverage,
    )
    if persist and drive_root is not None:
        repo_key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
        path = pathlib.Path(drive_root) / "state" / "code_intel" / repo_key / "inventory.json"
        atomic_write_json(path, inventory.to_json(), trailing_newline=True)
    return inventory


def render_codebase_digest(inventory: CodeInventory) -> str:
    lines: List[str] = []
    total_lines_est = 0
    total_symbols = 0
    for file in inventory.files:
        if file.disposition != "indexed":
            continue
        line_est = max(1, file.token_estimate // 20)
        total_lines_est += line_est
        total_symbols += len(file.symbols)
        parts = [f"\n== {file.path} ({file.size} bytes, {file.language}) =="]
        if file.symbols:
            names = ", ".join(symbol.name for symbol in file.symbols[:20])
            if len(file.symbols) > 20:
                names += f", ... ({len(file.symbols)} total)"
            parts.append(f"  Symbols: {names}")
        if file.imports:
            imports = ", ".join(file.imports[:12])
            if len(file.imports) > 12:
                imports += f", ... ({len(file.imports)} total)"
            parts.append(f"  Imports: {imports}")
        if file.routes:
            parts.append("  Routes: " + ", ".join(file.routes[:12]))
        lines.append("\n".join(parts))
    return (
        f"Codebase Digest ({len(inventory.files)} files, ~{total_lines_est} line-est, "
        f"{total_symbols} symbols, head={inventory.git_head[:12] or 'unknown'})\n"
        + "\n".join(lines)
    )
