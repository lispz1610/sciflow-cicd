#!/usr/bin/env python3
"""One credential-redaction pattern set for OCR, release, and SBOM diagnostics."""

from __future__ import annotations

import re
from typing import Any, Sequence, Tuple

PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 _-]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 _-]*PRIVATE KEY-----",
    re.MULTILINE,
)
PEM_PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN [A-Z0-9 _-]*PRIVATE KEY-----", re.MULTILINE)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
GITHUB_TOKEN = re.compile(r"\bgh[opusr]_[A-Za-z0-9_]{6,}\b", re.IGNORECASE)
GITHUB_FINE_GRAINED_TOKEN = re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}\b", re.IGNORECASE)
OPENAI_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b", re.IGNORECASE)
NVIDIA_TOKEN = re.compile(r"\bnvapi-[A-Za-z0-9_-]{10,}\b", re.IGNORECASE)
AWS_ACCESS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
KEY_VALUE_SECRET = re.compile(
    r"(?:api[_-]?key|client[_-]?secret|auth[_-]?token)\s*[:=]\s*[^\s,;'\"\]]{6,}",
    re.IGNORECASE,
)
AUTHORIZATION_HEADER = re.compile(r"(Authorization:\s*Bearer\s+)[^\s'\"]+", re.IGNORECASE)
BEARER_TOKEN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE)
URL_CREDENTIAL_PATTERN = re.compile(
    r"(https?://)([^:\s@]+):([^@\s/]+)@", re.IGNORECASE
)

_REDACTION_RULES: Tuple[Tuple[Any, str], ...] = (
    (PEM_PRIVATE_KEY, "[REDACTED]"),
    (PEM_PRIVATE_KEY_HEADER, "[REDACTED]"),
    (JWT, "[REDACTED]"),
    (GITHUB_TOKEN, "[REDACTED]"),
    (GITHUB_FINE_GRAINED_TOKEN, "[REDACTED]"),
    (OPENAI_TOKEN, "[REDACTED]"),
    (NVIDIA_TOKEN, "[REDACTED]"),
    (AWS_ACCESS_KEY, "[REDACTED]"),
    (KEY_VALUE_SECRET, "[REDACTED]"),
    (AUTHORIZATION_HEADER, r"\1[REDACTED]"),
    (BEARER_TOKEN, "[REDACTED]"),
    (URL_CREDENTIAL_PATTERN, r"\1[REDACTED]:[REDACTED]@"),
)

SECRET_PATTERNS: Tuple[Any, ...] = tuple(pattern for pattern, _ in _REDACTION_RULES)


def redact(text: str, extra_secrets: Sequence[str] = ()) -> str:
    """Redact every supported credential format from diagnostic text."""
    if not isinstance(text, str):
        return str(text)
    redacted = text
    for secret in extra_secrets:
        if isinstance(secret, str) and len(secret) >= 6:
            redacted = redacted.replace(secret, "[REDACTED]")
    for pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


redact_secrets = redact
