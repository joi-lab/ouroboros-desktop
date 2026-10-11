"""Marketplace clients, staging, provenance, and install orchestration.

Node/TypeScript OpenClaw plugins are intentionally unsupported: UI filters them
from search and install refuses them explicitly.
"""

import urllib.parse
import urllib.request
from typing import Callable, Iterable


class AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects to hosts outside a marketplace client's allowlist."""

    def __init__(self, allowed_hosts: Iterable[str], error_factory: Callable[[str], Exception]) -> None:
        super().__init__()
        self._allowed = frozenset(allowed_hosts)
        self._error = error_factory

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        target = urllib.parse.urlparse(newurl).hostname
        if target not in self._allowed:
            raise self._error(str(target))
        return super().redirect_request(req, fp, code, msg, headers, newurl)
