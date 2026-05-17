"""release_sync.py — deterministic release-metadata carrier sync helpers.

Shared release-metadata library used by both agent-facing workflows and
review gates. ``advisory_pre_review`` uses ``sync_release_metadata`` before
the Claude SDK call when VERSION is in scope, then runs deterministic P9
checks so release-carrier mistakes are caught before provider budget is spent.

Scope
-----
This library syncs the VERSION string across its three derived *carrier* files:
``pyproject.toml`` version field, ``README.md`` badge, and
``docs/ARCHITECTURE.md`` header.  It does NOT create or update the
``README.md`` Version History changelog row — that remains a manual authoring
surface because each release entry requires a human-written description.
``check_history_limit`` and ``detect_numeric_claims`` advise on the quality of
whatever row the author wrote, without modifying it.

Version format
--------------
Stable versions are plain semver (``X.Y.Z``). Pre-release versions use the
author-facing spellings ``4.50.0-rc.1`` / ``4.50.0rc1`` / ``4.50.0-alpha.2``
etc.; VERSION / README badge display / ARCHITECTURE header all keep the
author spelling verbatim, while ``pyproject.toml`` receives the PEP 440-
canonical form (``4.50.0rc1``) via ``_normalize_pep440`` so pip / build /
twine accept the project metadata. The README badge URL path additionally
doubles literal hyphens (``4.50.0--rc.1``) per shields.io's escape rule.

Public API
----------
sync_release_metadata(repo_dir)  -> list[str]   changed carrier file paths
check_history_limit(readme_text) -> list[str]   advisory P9 limit warnings
detect_numeric_claims(text)      -> list[str]   matched numeric-claim strings
run_release_preflight(repo_dir)  -> (list[str], list[str])  (changed, warnings)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

# ---------------------------------------------------------------------------
# P9 history limits (BIBLE.md)
# ---------------------------------------------------------------------------
_MAX_MAJOR = 2
_MAX_MINOR = 5
_MAX_PATCH = 5

# Numeric-claim pattern: stand-alone integer followed by a release noun.
# Matches "16 tests", "3 fixes", "42 new tests", etc.
_NUMERIC_CLAIM_RE = re.compile(
    r'\b(\d+)\s+(?:new\s+)?(?:\w+\s+)?(?:tests?|fixes?|checks?|functions?|lines?|changes?|regressions?|assertions?)\b',
    re.IGNORECASE,
)

# Optional PEP 440 pre-release suffix. Accepts the common author-facing
# spellings (``-rc.1``, ``-rc1``, ``rc.1``, ``rc1``, ``-alpha.2``,
# ``-beta.3``, ``-a.1``, ``-b.2``) and their case-insensitive variants.
# The display spelling is preserved verbatim in VERSION / README /
# ARCHITECTURE carriers; ``pyproject.toml`` is responsible for holding
# the PEP 440-canonical form (see sync_release_metadata docstring).
_PRE_SUFFIX = r'(?:-?(?:rc|alpha|beta|a|b)\.?\d+)?'

# Full semver+optional-pre token used for VERSION file validation.
_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+' + _PRE_SUFFIX + r'$', re.IGNORECASE)

# Version row in README Version History: "| X.Y.Z[-suffix] | date | description |"
# Pre-release rows bucket under their base version (a ``4.50.0-rc.1`` row
# counts as a minor row for the ``4.50.0`` slot, not a separate patch).
_VERSION_ROW_RE = re.compile(
    r'^\|\s*(\d+)\.(\d+)\.(\d+)' + _PRE_SUFFIX + r'\s*\|',
    re.MULTILINE | re.IGNORECASE,
)

# README badge display text + shields.io URL path. The URL path encodes
# literal ``-`` as ``--`` (shields.io path-segment escape) so the right-
# most ``-green`` separator stays unambiguous, hence the two halves of
# the regex accept slightly different tokens.
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

# ARCHITECTURE.md header: "# Ouroboros vX.Y.Z[-suffix] — ..."
_ARCH_HEADER_RE = re.compile(
    r'^(#\s+Ouroboros\s+v)'
    r'(\d+\.\d+\.\d+' + _PRE_SUFFIX + r')'
    r'(\s*)',
    re.MULTILINE | re.IGNORECASE,
)


def _shields_escape(version: str) -> str:
    """Escape a version string for a shields.io badge URL path segment.

    shields.io interprets ``-`` as a URL-path segment separator; literal
    hyphens inside a value (``4.50.0-rc.1``) must be doubled (``4.50.0--rc.1``)
    so the final ``-green`` ending stays unambiguous. Returns *version*
    unchanged when it contains no hyphen.
    """
    return version.replace('-', '--')


# Matches the pre-release tail (``-rc.1`` / ``rc1`` / ``-alpha.2`` / …)
# anchored at the right side of the full version string. Used by
# ``_normalize_pep440`` to split base and suffix without double-counting.
_PRE_TAIL_RE = re.compile(
    r'(-?)(rc|alpha|beta|a|b)(\.?)(\d+)$',
    re.IGNORECASE,
)


def _normalize_pep440(version: str) -> str:
    """Return the PEP 440-canonical spelling of *version*.

    Author-facing carriers (VERSION / README / ARCHITECTURE) tolerate
    ``4.50.0-rc.1`` style spellings because they read more naturally in
    prose and match the ``v{VERSION}`` git-tag convention. ``pyproject.toml``
    however is consumed by pip / build / twine, which enforce PEP 440:
    the canonical form for a release-candidate is ``4.50.0rc1`` (no
    separator between the base version and the pre-release identifier,
    no dot between ``rc`` and the number). This helper performs that
    normalisation so the VERSION file can carry the idiomatic spelling
    while ``pyproject.toml`` stays pip-compatible.

    Stable (non-RC) versions pass through unchanged.
    """
    match = _PRE_TAIL_RE.search(version)
    if not match:
        return version
    base = version[: match.start()]
    # PEP 440 §Pre-release spelling: ``alpha`` collapses to ``a``,
    # ``beta`` to ``b``, ``rc`` stays ``rc`` (``c`` is an alias pip also
    # accepts but the canonical short form is ``rc``). Every identifier
    # is lowercased on the way out.
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
    """Sync VERSION → pyproject.toml → README badge → ARCHITECTURE.md header.

    Reads the canonical version from the ``VERSION`` file and writes the
    correct value into the other three carriers when they are out of sync.

    Returns
    -------
    list[str]
        Repo-relative paths of files that were actually modified.
    """
    root = Path(repo_dir)
    version_file = root / "VERSION"
    if not version_file.exists():
        return []

    version = version_file.read_text(encoding="utf-8").strip()
    if not _VERSION_RE.match(version):
        return []

    # ``pyproject.toml`` must carry the PEP 440-canonical form so pip
    # / twine / build do not reject the project metadata. Author-facing
    # spellings like ``4.50.0-rc.1`` normalise to ``4.50.0rc1`` per
    # PEP 440 §5 (pre-release separator and identifier rules); we do
    # the normalisation here rather than requiring the VERSION file to
    # use the canonical form because the hyphen/dot variant is more
    # idiomatic in README / ARCHITECTURE prose.
    pyproject_version = _normalize_pep440(version)
    # README badge URL path segment doubles literal ``-`` (shields.io
    # escape) but the badge display text keeps the author spelling.
    badge_url_version = _shields_escape(version)

    changed: List[str] = []

    # --- pyproject.toml ---
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

    # --- README.md badge ---
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

    # --- docs/ARCHITECTURE.md header ---
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
    """Return advisory warnings when Version History exceeds P9 limits.

    Limits: 2 major, 5 minor, 5 patch rows visible in the history table.
    Never raises — always returns a (possibly empty) list of warning strings.
    """
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
    """Return matched numeric-claim strings found in *text*.

    Examples: ``"16 tests"``, ``"3 new fixes"``, ``"42 regression tests"``.
    Advisory only — callers decide how to surface these.
    """
    return [m.group(0) for m in _NUMERIC_CLAIM_RE.finditer(text)]


def run_release_preflight(repo_dir: str) -> Tuple[List[str], List[str]]:
    """Run all release-sync checks and return (changed_files, advisory_warnings).

    1. Sync VERSION carriers (pyproject.toml, README badge, ARCHITECTURE header).
    2. Check Version History limits.
    3. Detect numeric claims in the current README changelog row for the new version.

    This function is idempotent: running it twice in a row produces no further
    changes on the second call (assuming no external modifications between calls).

    Returns
    -------
    (changed_files, advisory_warnings)
        *changed_files* — repo-relative paths actually written.
        *advisory_warnings* — non-blocking strings describing policy violations.
    """
    changed = sync_release_metadata(repo_dir)

    warnings: List[str] = []
    readme = Path(repo_dir) / "README.md"
    if readme.exists():
        readme_text = readme.read_text(encoding="utf-8")
        warnings.extend(check_history_limit(readme_text))

        # Find the changelog row for the current VERSION and flag numeric claims.
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
