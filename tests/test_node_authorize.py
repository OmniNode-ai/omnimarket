# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_authorize [OMN-9104].

Covers DoD:
- write: skill dispatch produces $ONEX_STATE_DIR/session/authorization.json
  matching the ModelAgentAuthorizationGrant schema read by Task 3 hook.
- expire: reader contract — a grant whose expires_at is in the past is
  treated as "no grant" (node still writes it; semantics live in the model).
- overwrite: a second invocation replaces the prior file contents.
- atomic: the writer must never leave a partially-written file observable
  (tempfile + os.replace pattern, as in node_session_bootstrap.dispatch_lease).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_authorize.handlers.handler_authorize import (
    AuthorizeRequest,
    HandlerAuthorize,
)
from omnimarket.nodes.node_authorize.models.model_agent_authorization_grant import (
    ModelAgentAuthorizationGrant,
    load_grant_if_valid,
)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))
    return tmp_path


def _auth_file(state_dir: Path) -> Path:
    return state_dir / "session" / "authorization.json"


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def test_handler_writes_grant_to_canonical_path(state_dir: Path) -> None:
    handler = HandlerAuthorize()
    request = AuthorizeRequest(
        scope=["src/**", "tests/**"],
        tools=["Edit", "Write"],
        ttl_seconds=3600,
    )

    result = handler.handle(request)

    path = _auth_file(state_dir)
    assert path.is_file(), "authorize node must write authorization.json"
    assert result.path == str(path)

    data = json.loads(path.read_text())
    assert data["scope"] == ["src/**", "tests/**"]
    assert data["tools"] == ["Edit", "Write"]
    assert data["granted_at"].endswith("+00:00") or data["granted_at"].endswith("Z")
    assert data["expires_at"] is not None

    # Round-trip through the model the reader uses.
    grant = ModelAgentAuthorizationGrant.model_validate(data)
    assert grant.scope == ("src/**", "tests/**")
    assert grant.tools == ("Edit", "Write")
    assert grant.expires_at is not None
    assert grant.expires_at > grant.granted_at


def test_handler_writes_null_expires_for_ttl_none(state_dir: Path) -> None:
    handler = HandlerAuthorize()
    request = AuthorizeRequest(
        scope=["docs/**"],
        tools=["Edit"],
        ttl_seconds=None,
    )

    handler.handle(request)

    data = json.loads(_auth_file(state_dir).read_text())
    assert data["expires_at"] is None, (
        "ttl_seconds=None must yield non-expiring grant per schema"
    )


# ---------------------------------------------------------------------------
# expire
# ---------------------------------------------------------------------------


def test_expired_grant_is_treated_as_no_grant_by_reader(state_dir: Path) -> None:
    """Contract with Task 3 hook reader: expires_at < now() ⇒ None."""
    path = _auth_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    past = datetime.now(UTC) - timedelta(seconds=1)
    payload = {
        "scope": ["src/**"],
        "granted_at": (past - timedelta(hours=1)).isoformat(),
        "expires_at": past.isoformat(),
        "tools": ["Edit"],
    }
    path.write_text(json.dumps(payload))

    assert load_grant_if_valid(path) is None


def test_missing_file_returns_none(state_dir: Path) -> None:
    assert load_grant_if_valid(_auth_file(state_dir)) is None


def test_non_expiring_grant_is_valid(state_dir: Path) -> None:
    path = _auth_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scope": ["src/**"],
        "granted_at": datetime.now(UTC).isoformat(),
        "expires_at": None,
        "tools": ["Edit"],
    }
    path.write_text(json.dumps(payload))

    grant = load_grant_if_valid(path)
    assert grant is not None
    assert grant.expires_at is None


# ---------------------------------------------------------------------------
# overwrite
# ---------------------------------------------------------------------------


def test_second_write_replaces_prior_grant(state_dir: Path) -> None:
    handler = HandlerAuthorize()

    handler.handle(AuthorizeRequest(scope=["src/**"], tools=["Edit"], ttl_seconds=60))
    handler.handle(
        AuthorizeRequest(
            scope=["tests/**", "docs/**"],
            tools=["Edit", "Write"],
            ttl_seconds=120,
        )
    )

    data = json.loads(_auth_file(state_dir).read_text())
    assert data["scope"] == ["tests/**", "docs/**"]
    assert data["tools"] == ["Edit", "Write"]


# ---------------------------------------------------------------------------
# atomic
# ---------------------------------------------------------------------------


def test_writer_leaves_no_tempfile_behind_on_success(state_dir: Path) -> None:
    """Atomic write via tempfile + os.replace — no *.tmp stragglers."""
    handler = HandlerAuthorize()
    handler.handle(AuthorizeRequest(scope=["src/**"], tools=["Edit"], ttl_seconds=60))

    session_dir = state_dir / "session"
    stragglers = [p for p in session_dir.iterdir() if p.suffix == ".tmp"]
    assert stragglers == [], f"unexpected tempfile remnants: {stragglers}"


def test_write_failure_leaves_no_partial_file(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace blows up, no partial authorization.json must exist."""
    handler = HandlerAuthorize()

    real_replace = os.replace

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        handler.handle(
            AuthorizeRequest(scope=["src/**"], tools=["Edit"], ttl_seconds=60)
        )

    assert not _auth_file(state_dir).exists(), (
        "partial write must not leave authorization.json on disk"
    )

    # Restore and verify cleanup on subsequent success.
    monkeypatch.setattr(os, "replace", real_replace)
    handler.handle(AuthorizeRequest(scope=["src/**"], tools=["Edit"], ttl_seconds=60))
    assert _auth_file(state_dir).is_file()


# ---------------------------------------------------------------------------
# env guard
# ---------------------------------------------------------------------------


def test_missing_onex_state_dir_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    handler = HandlerAuthorize()
    with pytest.raises(RuntimeError, match="ONEX_STATE_DIR"):
        handler.handle(
            AuthorizeRequest(scope=["src/**"], tools=["Edit"], ttl_seconds=60)
        )


# ---------------------------------------------------------------------------
# CodeRabbit regression (PR #324 review)
# ---------------------------------------------------------------------------


def test_naive_datetime_in_grant_rejected_by_validator(state_dir: Path) -> None:
    """Naive datetimes on disk must fail model validation, not TypeError later.

    Without the tz-aware validator, is_expired() would raise TypeError
    comparing naive vs aware datetimes, and that TypeError would escape the
    load_grant_if_valid() catch-all (which only handles OSError, JSONDecodeError,
    ValueError). Reader contract says missing/malformed → None; enforce it
    at validation time so a real bug surfaces via Pydantic ValidationError.
    """
    path = _auth_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scope": ["src/**"],
        "granted_at": "2026-04-17T10:00:00",  # no tz suffix
        "expires_at": "2026-04-17T14:00:00",
        "tools": ["Edit"],
    }
    path.write_text(json.dumps(payload))

    assert load_grant_if_valid(path) is None, (
        "malformed-timestamps grant must collapse to None via reader contract"
    )


def test_short_write_is_detected_and_does_not_publish_truncated_grant(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.write returning 0 must abort before os.replace publishes a partial."""
    handler = HandlerAuthorize()

    def zero_write(fd: int, data: bytes) -> int:
        return 0

    monkeypatch.setattr(os, "write", zero_write)

    with pytest.raises(OSError, match="returned 0"):
        handler.handle(
            AuthorizeRequest(scope=["src/**"], tools=["Edit"], ttl_seconds=60)
        )
    assert not _auth_file(state_dir).exists(), (
        "truncated-write must never promote to authorization.json"
    )
