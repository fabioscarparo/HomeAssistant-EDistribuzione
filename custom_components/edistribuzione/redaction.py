"""Privacy helpers for logs, exceptions and diagnostics.

Pure module with no Home Assistant dependencies, so auth.py and api.py stay
usable standalone.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

_RE_POD = re.compile(r"\b(IT\d{3}E)([A-Za-z0-9]{2,})([A-Za-z0-9]{4})\b", re.IGNORECASE)
_RE_BEARER = re.compile(r"(Bearer\s+)[^\s,'\"<>]+", re.IGNORECASE)
_RE_QUERY_SECRET = re.compile(
    r"((?:^|[?&#;\s'\"])"
    r"(?:sid|code|access_token|refresh_token|id_token|password|login_hint|state|code_verifier)"
    r"\s*=\s*)[^&\s'\"<>]+",
    re.IGNORECASE,
)
_RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_RE_CODICE_FISCALE = re.compile(
    r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE
)


def mask_pod(pod: str) -> str:
    """Keep prefix and last 4 chars so multi-POD logs remain distinguishable."""
    if not pod:
        return pod
    match = _RE_POD.fullmatch(pod)
    if match:
        return f"{match.group(1).upper()}****{match.group(3).upper()}"
    if len(pod) > 6:
        return f"{pod[:2]}****{pod[-4:]}"
    return "****"


def mask_pods_in_text(text: str) -> str:
    """Mask every Italian POD code appearing inside free text."""
    return _RE_POD.sub(lambda m: f"{m.group(1).upper()}****{m.group(3).upper()}", text)


def redact_url(url: Any) -> str:
    """Drop query and fragment so session ids, codes and tokens are never logged."""
    parts = urlsplit(str(url))
    if not parts.scheme and not parts.netloc:
        return parts.path
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def redact_secrets(text: str) -> str:
    """Scrub tokens, OAuth params, emails and tax codes from free text."""
    cleaned = _RE_BEARER.sub(r"\1[REDACTED]", text)
    cleaned = _RE_QUERY_SECRET.sub(r"\1[REDACTED]", cleaned)
    cleaned = _RE_EMAIL.sub("[REDACTED_EMAIL]", cleaned)
    return _RE_CODICE_FISCALE.sub("[REDACTED_CF]", cleaned)


def redact_params(params: dict[str, Any]) -> dict[str, Any]:
    """Return a log-safe copy of query parameters."""
    safe: dict[str, Any] = {}
    for key, value in params.items():
        if key.lower() == "pointofdelivery" and isinstance(value, str):
            safe[key] = mask_pod(value)
        else:
            safe[key] = value
    return safe


def redact_preview(text: str, max_len: int = 200) -> str:
    """Return a short, scrubbed preview of a raw server payload."""
    snippet = text[:max_len]
    return mask_pods_in_text(redact_secrets(snippet))
