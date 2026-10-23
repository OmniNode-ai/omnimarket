# SPDX-License-Identifier: MIT
"""Credential-safe rendering of process failures for PR remediation."""

from __future__ import annotations

import re

_CREDENTIAL_URL_RE = re.compile(r"(x-access-token:)[^@\s]+(@)")
PROCESS_OUTPUT_RENDER_LIMIT = 2000


def scrub_credentials(text: str) -> str:
    """Redact any ``x-access-token:<secret>@`` credential from ``text``."""
    if not text:
        return text
    return _CREDENTIAL_URL_RE.sub(r"\1***\2", text)


def _scrub_process_text(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return scrub_credentials(value) or None


def _render_stream(label: str, value: str | bytes | None) -> str:
    """Render one captured stream, scrubbed and bounded, or "" when empty."""
    text = _scrub_process_text(value)
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""
    if len(text) > PROCESS_OUTPUT_RENDER_LIMIT:
        dropped = len(text) - PROCESS_OUTPUT_RENDER_LIMIT
        text = f"{text[:PROCESS_OUTPUT_RENDER_LIMIT]}… [truncated {dropped} chars]"
    return f" | {label}: {text}"


def format_process_error(exc: BaseException) -> str:
    """Render a process failure with bounded, credential-scrubbed streams."""
    base = scrub_credentials(str(exc))
    stdout = getattr(exc, "stdout", None)
    stderr = getattr(exc, "stderr", None)
    return f"{base}{_render_stream('stdout', stdout)}{_render_stream('stderr', stderr)}"
