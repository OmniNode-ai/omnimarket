# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Key splitting and status-to-error mapping for the Infisical adapter."""

from __future__ import annotations

from typing import NoReturn


def split_key(key: str) -> tuple[str, str]:
    """``/a/b/NAME`` into (``/a/b``, ``NAME``); a bare ``NAME`` is in ``/``.

    Raises:
        ValueError: an empty name or a name with surrounding whitespace.
    """
    folder, _, name = key.rpartition("/")
    if not name or name != name.strip():
        raise ValueError("a secret key needs a non-empty name with no padding")
    return (folder or "/"), name


def raise_for_status(status: int, operation: str) -> NoReturn:
    """Raise the typed error for an HTTP status (never a response body)."""
    if status in (401, 403):
        raise PermissionError(f"{operation} refused with HTTP {status}")
    if status == 429 or status >= 500:
        raise ConnectionError(f"{operation} unavailable with HTTP {status}")
    raise ValueError(f"{operation} rejected with HTTP {status}")
