"""Deterministic release metadata sync and P9 preflight helpers.

VERSION remains canonical for author-facing carriers; pyproject receives PEP
440 spelling, README badge URL escapes hyphens, and changelog prose stays manual.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

_MAX_MAJOR = 2
_MAX_MINOR = 5
_MAX_PATCH = 5

# Stand-alone integer followed by release-count nouns.
_NUMERIC_CLAIM_RE = re.compile(
    r'\b(\d+)\s+(?:new\s+)?(?:\w+\s+)?(?:tests?|fixes?|checks?|functions?|lines?|changes?|regressions?|assertions?)\b',
    re.IGNORECASE,
)

# Author-facing pre-release suffix; pyproject gets the PEP 440-normalized form.
_PRE_SUFFIX = r'(?:-?(?:rc|alpha|beta|a|b)\.?\d+)?'

_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+' + _PRE_SUFFIX + r'$', re.IGNORECASE)

# README Version History row; pre-release rows bucket under their base version.
_VERSION_ROW_RE = re.compile(
    r'^\|\s*(\d+)\.(\d+)\.(\d+)' + _PRE_SUFFIX + r'\s*\|',
    re.MULTILINE | re.IGNORECASE,
)

# Badge display keeps VERSION spelling; URL path doubles hyphens for shields.io.
_BADGE_DISPLAY_TOKEN = r'\d+\.\d+\.\d+' + _PRE_SUFFIX
_BADGE_URL_TOKEN = (
    r'\d+\.\d+\.\d+'
    r'(?:(?:-{1,2})?(?:rc|alpha|beta|a|b)\.?\d+)?'
)
_README_BADGE_RE = re.compile(
    r'(\[!\[Version\s+)'
    r'(' + _BADGE_DISPLAY_TOKEN + r')'
    r'(\]\(https://img\.shields\.io/badge/version-)'
    r'(' + _BADGE_URL_TOKEN + r')'
    r'(-green\.svg\)\])',
    re.IGNORECASE,
)

_ARCH_HEADER_RE = re.compile(
    r'^(#\s+Ouroboros\s+v)'
    r'(\d+\.\d+\.\d+' + _PRE_SUFFIX + r')'
    r'(\s*)',
    re.MULTILINE | re.IGNORECASE,
)


def _shields_escape(version: str) -> str:
    """Double literal hyphens so shields.io keeps them inside the value segment."""
    return version.replace('-', '--')


# Pre-release tail anchored at the right side for PEP 440 normalization.
_PRE_TAIL_RE = re.compile(
    r'(-?)(rc|alpha|beta|a|b)(\.?)(\d+)$',
    re.IGNORECASE,
)


def _normalize_pep440(version: str) -> str:
    """Return PEP 440 spelling for pyproject while stable versions pass through."""
    match = _PRE_TAIL_RE.search(version)
    if not match:
        return version
    base = version[: match.start()]
    identifier_raw = match.group(2).lower()
    _pep440_alias = {"alpha": "a", "beta": "b"}
    identifier = _pep440_alias.get(identifier_raw, identifier_raw)
    number = match.group(4)
    return f"{base}{identifier}{number}"


def is_release_version(version: str) -> bool:
    """Return True when *version* matches the supported release grammar."""
    return bool(_VERSION_RE.match(str(version or "").strip()))


def normalize_release_tag(tag: str) -> str:
    """Return canonical ``v{VERSION}`` spelling or ``""`` for non-release tags."""
    raw = str(tag or "").strip()
    if not raw:
        return ""
    version = raw[1:] if raw.lower().startswith("v") else raw
    if not is_release_version(version):
        return ""
    return f"v{version}"


def extract_readme_badge_version(readme_text: str) -> str:
    """Extract the display version from the README badge, if present."""
    match = _README_BADGE_RE.search(str(readme_text or ""))
    return str(match.group(2) or "").strip() if match else ""


def extract_architecture_header_version(arch_text: str) -> str:
    """Extract the version token from the ARCHITECTURE.md header, if present."""
    match = _ARCH_HEADER_RE.search(str(arch_text or ""))
    return str(match.group(2) or "").strip() if match else ""


def version_carrier_desyncs(
    version: str,
    *,
    pyproject_text: str = "",
    readme_text: str = "",
    arch_text: str = "",
    detailed: bool = False,
) -> List[str]:
    """Return release-carrier mismatch labels for already-read file contents."""
    version = str(version or "").strip()
    if not is_release_version(version):
        return []
    desync: List[str] = []
    if pyproject_text:
        match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject_text, re.MULTILINE)
        expected = _normalize_pep440(version)
        if not match or match.group(1).strip() != expected:
            desync.append(f'pyproject.toml (expected version = "{expected}")' if detailed else "pyproject.toml")
    if readme_text:
        badge_token = f"version-{_shields_escape(version)}-green"
        if extract_readme_badge_version(readme_text) != version or badge_token not in readme_text:
            desync.append(f"README.md badge (expected {version} / {badge_token})" if detailed else "README.md badge")
    if arch_text and extract_architecture_header_version(arch_text) != version:
        desync.append(f"docs/ARCHITECTURE.md header (expected # Ouroboros v{version})" if detailed else "ARCHITECTURE.md header")
    return desync


def sync_release_metadata(repo_dir: str) -> List[str]:
    """Sync VERSION into pyproject, README badge, and ARCHITECTURE header."""
    root = Path(repo_dir)
    version_file = root / "VERSION"
    if not version_file.exists():
        return []

    version = version_file.read_text(encoding="utf-8").strip()
    if not _VERSION_RE.match(version):
        return []

    # pyproject must be PEP 440; author-facing carriers keep VERSION spelling.
    pyproject_version = _normalize_pep440(version)
    badge_url_version = _shields_escape(version)

    changed: List[str] = []

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        new_text = re.sub(
            r'^(version\s*=\s*")[^"]*(")',
            lambda m: f'{m.group(1)}{pyproject_version}{m.group(2)}',
            text,
            flags=re.MULTILINE,
        )
        if new_text != text:
            pyproject.write_text(new_text, encoding="utf-8")
            changed.append("pyproject.toml")

    readme = root / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        new_text = _README_BADGE_RE.sub(
            lambda m: (
                m.group(1) + version + m.group(3) + badge_url_version + m.group(5)
            ),
            text,
        )
        if new_text != text:
            readme.write_text(new_text, encoding="utf-8")
            changed.append("README.md")

    arch = root / "docs" / "ARCHITECTURE.md"
    if arch.exists():
        text = arch.read_text(encoding="utf-8")
        new_text = _ARCH_HEADER_RE.sub(
            lambda m: m.group(1) + version + m.group(3),
            text,
        )
        if new_text != text:
            arch.write_text(new_text, encoding="utf-8")
            changed.append("docs/ARCHITECTURE.md")

    return changed


def check_history_limit(readme_text: str) -> List[str]:
    """Return advisory warnings when Version History exceeds P9 row limits."""
    warnings: List[str] = []
    major_rows, minor_rows, patch_rows = 0, 0, 0

    for m in _VERSION_ROW_RE.finditer(readme_text):
        _, min_, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if min_ == 0 and patch == 0:
            major_rows += 1
        elif patch == 0:
            minor_rows += 1
        else:
            patch_rows += 1

    if major_rows > _MAX_MAJOR:
        warnings.append(
            f"Version History has {major_rows} major rows (limit {_MAX_MAJOR}): "
            f"trim oldest major entries."
        )
    if minor_rows > _MAX_MINOR:
        warnings.append(
            f"Version History has {minor_rows} minor rows (limit {_MAX_MINOR}): "
            f"trim oldest minor entries."
        )
    if patch_rows > _MAX_PATCH:
        warnings.append(
            f"Version History has {patch_rows} patch rows (limit {_MAX_PATCH}): "
            f"trim oldest patch entries."
        )
    return warnings


def detect_numeric_claims(text: str) -> List[str]:
    """Return matched numeric-claim strings found in changelog prose."""
    return [m.group(0) for m in _NUMERIC_CLAIM_RE.finditer(text)]


def run_release_preflight(repo_dir: str) -> Tuple[List[str], List[str]]:
    """Run idempotent carrier sync plus advisory release-history checks."""
    changed = sync_release_metadata(repo_dir)

    warnings: List[str] = []
    readme = Path(repo_dir) / "README.md"
    if readme.exists():
        readme_text = readme.read_text(encoding="utf-8")
        warnings.extend(check_history_limit(readme_text))

        # Flag numeric claims only in the current VERSION row.
        version_file = Path(repo_dir) / "VERSION"
        if version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
            row_re = re.compile(
                r'^\|\s*' + re.escape(version) + r'\s*\|[^|]*\|([^|]+)\|?\s*$',
                re.MULTILINE,
            )
            m = row_re.search(readme_text)
            if m:
                claims = detect_numeric_claims(m.group(1))
                if claims:
                    warnings.append(
                        f"Changelog row for {version} contains numeric claims that "
                        f"may become stale: {claims!r}. Consider replacing with "
                        f"descriptive language."
                    )

    return changed, warnings
