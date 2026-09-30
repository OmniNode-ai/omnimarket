# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure credential redaction before snapshot text enters a label event."""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"
_PATTERNS = (
    re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"(?i)\bx-access-token:[^\s/@]+@"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9_]+|xox[a-zA-Z]-[A-Za-z0-9_-]+|AKIA[A-Z0-9]{16})\b"
    ),
    re.compile(
        r"""(?i)\b(?:password|passwd|api[_-]?key|secret|client_secret|access_token|auth_token|token)["']?\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;&]+)"""
    ),
)


def scrub_snapshot(text: str) -> str:
    """Remove recognized credentials while preserving surrounding prose."""
    for pattern in _PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text
