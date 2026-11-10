# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The assign operation of a host's Claude token pool: an operator names a token, the login follows.

The same fakes as the rotation tick's tests: every token is fake and assembled at run time, the pool
is the real handler over a temporary local store, the login file is a real file. No result, line or
event carries a token: labels and fingerprints only. The failing control names a limited label and
shows the refusal is the control: the login file is byte-identical afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    fingerprint,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_rotation_tick import (
    HandlerClaudeTokenAssign,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenAssignRequest,
    ModelClaudeTokenAssignResult,
    ModelRotationEvent,
)
from tests.nodes.node_claude_token_pool_effect.test_claude_token_rotation_tick import (
    LABELS,
    Rig,
    fake_token,
)

pytestmark = pytest.mark.unit


def assign(
    rig: Rig, label: str, *, dry_run: bool = False
) -> ModelClaudeTokenAssignResult:
    """One assign on the rig's pool and login file, with the rig's clock and events file."""
    return HandlerClaudeTokenAssign(
        pool=rig.pool,
        token_file=rig.token_file,
        host=rig.host,
        events_path=rig.events,
        now=rig.clock,
    ).handle(ModelClaudeTokenAssignRequest(host=rig.host, label=label, dry_run=dry_run))


def test_assign_moves_the_login_to_the_named_token(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    result = assign(rig, "acct-b")
    assert result.outcome == "assigned"
    assert result.refusal == "none"
    assert result.from_label == "acct-a"
    assert rig.in_use() == "acct-b"
    assert rig.token_file.fingerprint() == fingerprint(fake_token("acct-b"))
    rows = [
        ModelRotationEvent.model_validate_json(line)
        for line in rig.events.read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0].reason == "assigned"
    assert rows[0].host == "h101"
    assert rows[0].from_label == "acct-a"
    assert rows[0].to_label == "acct-b"


def test_assigning_the_token_already_in_use_is_unchanged(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    before = rig.token_path.stat().st_mtime_ns
    result = assign(rig, "acct-a")
    assert result.outcome == "unchanged"
    assert result.refusal == "none"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.stat().st_mtime_ns == before
    assert not rig.events.exists()


def test_a_dry_run_assign_plans_without_writing(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    before = rig.token_path.read_bytes()
    result = assign(rig, "acct-b", dry_run=True)
    assert result.outcome == "assigned"
    assert result.from_label == "acct-a"
    assert any("would assign" in line for line in result.lines)
    assert not rig.events.exists()
    assert rig.token_path.read_bytes() == before


def test_assign_refuses_a_limited_label(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    before = rig.token_path.read_bytes()
    rig.limit("caia")
    result = assign(rig, "caia")
    assert result.outcome == "refused"
    assert result.refusal == "limited"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.read_bytes() == before
    assert not rig.events.exists()


def test_assign_refuses_a_revoked_label(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    before = rig.token_path.read_bytes()
    rig.revoke("onexbot")
    result = assign(rig, "onexbot")
    assert result.outcome == "refused"
    assert result.refusal == "revoked"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.read_bytes() == before
    assert not rig.events.exists()


def test_assign_refuses_a_label_not_in_the_pool(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    before = rig.token_path.read_bytes()
    result = assign(rig, "ghost")
    assert result.outcome == "refused"
    assert result.refusal == "unknown_label"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.read_bytes() == before
    assert not rig.events.exists()


def test_no_token_text_in_any_result_or_event(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    results = [
        assign(rig, "acct-b"),
        assign(rig, "caia", dry_run=True),
    ]
    rig.limit("caia")
    results.append(assign(rig, "caia"))
    blob = "\n".join(r.model_dump_json() + "\n".join(r.lines) for r in results)
    if rig.events.exists():
        blob += "\n" + rig.events.read_text()
    for label in LABELS:
        assert fake_token(label) not in blob


def test_control_assign_refuses_a_limited_label_even_when_it_is_the_only_one_named(
    tmp_path: Path,
) -> None:
    # the refusal is the control: a limited label is refused even when it is the only one named,
    # and the login file is byte-identical afterwards
    rig = Rig(tmp_path)
    rig.login("acct-a")
    before = rig.token_path.read_bytes()
    rig.limit("caia")
    result = assign(rig, "caia")
    assert result.outcome == "refused"
    assert result.refusal == "limited"
    assert rig.token_path.read_bytes() == before
    assert not rig.events.exists()
