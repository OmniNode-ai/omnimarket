# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_authorize.

WS-5 Wave 7 (OMN-13681). EFFECT archetype -> Variant A: the handler is invoked
in-process and the typed ``AuthorizeResult`` plus the on-disk grant (round-tripped
through ``load_grant_if_valid``) is asserted. The only I/O boundary is the
filesystem, exercised against a real ``ONEX_STATE_DIR`` under ``tmp_path`` — no
monkeypatching of os primitives.

Param axes (>=3 distinct sets + a negative control):
  * non-expiring grant (ttl=None) -> expires_at null, reads back valid.
  * future TTL -> expires_at in the future, reads back valid.
  * multi-scope / multi-tool -> grant carries every scope + tool entry.
  * zero TTL -> grant is written but immediately expired, so the reader rejects
    it (NEGATIVE CONTROL: a known-bad grant produces a "no grant" finding).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_authorize.handlers.handler_authorize import (
    AuthorizeRequest,
    HandlerAuthorize,
)
from omnimarket.nodes.node_authorize.models.model_agent_authorization_grant import (
    AUTHORIZATION_FILE_RELATIVE_PATH,
    load_grant_if_valid,
)

# (case_id, request_kwargs, expect)
_CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "non-expiring",
        {"scope": ["src/**"], "tools": ["Edit"], "ttl_seconds": None},
        {"expires": False, "reads_valid": True, "n_scope": 1, "n_tools": 1},
    ),
    (
        "future-ttl",
        {"scope": ["src/**"], "tools": ["Edit", "Write"], "ttl_seconds": 3600},
        {"expires": True, "reads_valid": True, "n_scope": 1, "n_tools": 2},
    ),
    (
        "multi-scope-multi-tool",
        {
            "scope": ["src/**", "tests/**", "docs/**"],
            "tools": ["Edit", "Write", "NotebookEdit"],
            "ttl_seconds": 600,
        },
        {"expires": True, "reads_valid": True, "n_scope": 3, "n_tools": 3},
    ),
    (
        "zero-ttl-immediately-expired",  # NEGATIVE CONTROL
        {"scope": ["src/**"], "tools": ["Edit"], "ttl_seconds": 0},
        {"expires": True, "reads_valid": False, "n_scope": 1, "n_tools": 1},
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("case_id", "request_kwargs", "expect"),
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_authorize_writes_and_reads_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    request_kwargs: dict[str, Any],
    expect: dict[str, Any],
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path))

    result = HandlerAuthorize().handle(AuthorizeRequest(**request_kwargs))

    grant_path = tmp_path / AUTHORIZATION_FILE_RELATIVE_PATH
    assert Path(result.path) == grant_path
    assert grant_path.is_file(), f"{case_id}: grant file must exist"

    if expect["expires"]:
        assert result.expires_at is not None
    else:
        assert result.expires_at is None

    # Round-trip through the reader-side contract used by the omniclaude hook.
    loaded = load_grant_if_valid(grant_path)
    if expect["reads_valid"]:
        assert loaded is not None, f"{case_id}: grant must read back as valid"
        assert len(loaded.scope) == expect["n_scope"]
        assert len(loaded.tools) == expect["n_tools"]
        assert list(loaded.scope) == request_kwargs["scope"]
        assert list(loaded.tools) == request_kwargs["tools"]
    else:
        # The bad fixture (already-expired grant) collapses to "no grant".
        assert loaded is None, f"{case_id}: expired grant must NOT read back"


@pytest.mark.integration
def test_authorize_fails_fast_without_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEGATIVE CONTROL: missing ONEX_STATE_DIR must fail loud, not silently default."""
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    with pytest.raises(RuntimeError, match="ONEX_STATE_DIR is not set"):
        HandlerAuthorize().handle(
            AuthorizeRequest(scope=["src/**"], tools=["Edit"], ttl_seconds=None)
        )
