# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAuthorize — write ModelAgentAuthorizationGrant atomically.

Side-effect node: writes ``$ONEX_STATE_DIR/session/authorization.json``
using the temp-file + ``os.replace`` pattern so readers (Task 3 hook)
never observe a partial file.

Fail-fast on missing ``ONEX_STATE_DIR`` per the omni_home operating rules —
silent defaulting routes writes to the wrong directory.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_authorize.models.model_agent_authorization_grant import (
    AUTHORIZATION_FILE_RELATIVE_PATH,
    ModelAgentAuthorizationGrant,
)

logger = logging.getLogger(__name__)


class AuthorizeRequest(BaseModel):
    """Input for the authorize handler.

    ``ttl_seconds=None`` produces a non-expiring grant (``expires_at: null``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: list[str] = Field(..., min_length=1)
    tools: list[str] = Field(..., min_length=1)
    ttl_seconds: int | None = None


class AuthorizeResult(BaseModel):
    """Output of the authorize handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    granted_at: datetime
    expires_at: datetime | None


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data`` to ``fd`` — os.write may short-write.

    Publishing a truncated JSON grant via os.replace would break the reader
    contract (load_grant_if_valid would see malformed JSON and return None,
    which silently degrades to "no grant" instead of loud failure).
    """
    view = memoryview(data)
    total = 0
    while total < len(view):
        written = os.write(fd, view[total:])
        if written == 0:
            raise OSError(
                f"os.write returned 0 after writing {total}/{len(data)} bytes "
                "to authorization tempfile; refusing to publish truncated grant"
            )
        total += written


def _resolve_authorization_path() -> Path:
    state_dir = os.environ.get("ONEX_STATE_DIR")
    if not state_dir:
        raise RuntimeError(
            "ONEX_STATE_DIR is not set; authorize node cannot write "
            "authorization.json. Set ONEX_STATE_DIR before invoking."
        )
    return Path(state_dir) / AUTHORIZATION_FILE_RELATIVE_PATH


class HandlerAuthorize:
    """Write a ModelAgentAuthorizationGrant to the session state directory.

    ONEX node archetype: EFFECT. Not idempotent — each invocation replaces
    any prior grant.
    """

    def handle(self, request: AuthorizeRequest) -> AuthorizeResult:
        path = _resolve_authorization_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        granted_at = datetime.now(UTC)
        expires_at: datetime | None = (
            granted_at + timedelta(seconds=request.ttl_seconds)
            if request.ttl_seconds is not None
            else None
        )

        grant = ModelAgentAuthorizationGrant(
            scope=tuple(request.scope),
            granted_at=granted_at,
            expires_at=expires_at,
            tools=tuple(request.tools),
        )
        payload = grant.model_dump(mode="json")
        payload_bytes = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")

        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=".authorization-", suffix=".tmp"
        )
        try:
            try:
                _write_all(fd, payload_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_path, path)
        except Exception:
            with suppress(OSError):
                os.unlink(tmp_path)
            raise

        logger.info(
            "authorization grant written: path=%s tools=%s scope_entries=%d expires=%s",
            path,
            list(grant.tools),
            len(grant.scope),
            expires_at.isoformat() if expires_at else "never",
        )
        return AuthorizeResult(
            path=str(path),
            granted_at=granted_at,
            expires_at=expires_at,
        )
