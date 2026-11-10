# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The spread rule of a host's Claude token pool: an aged, used login moves to an unused token (OMN-23550).

The same fakes as the rotation tick's tests: every token is fake and assembled at run time, the pool
is the real handler over a temporary local store, the login file is a real file whose age is set
with ``os.utime`` against the rig's clock that starts at T0. The failing control switches the spread
rule off and shows the aged, used login stays where it is; a named injected break (the spread step
removed from the tick) turns the spread outcome assertion red.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    ClaudeTokenPoolRefusedError,
    fingerprint,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_rotation_tick import (
    HandlerClaudeTokenRotation,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenRotationRequest,
    ModelRotationEvent,
    ModelRotationSpread,
)
from tests.nodes.node_claude_token_pool_effect.test_claude_token_rotation_tick import (
    T0,
    Rig,
    fake_token,
    overlay,
)

pytestmark = pytest.mark.unit

#: the spread bound, and how much older than it the login file is aged
BOUND_SECONDS = 3600
AGE_SECONDS = 7200


def spread_rig(tmp: Path, *, enabled: bool = True) -> Rig:
    """The shared rig, its tick handler rebuilt on an overlay that declares the spread rule."""
    rig = Rig(tmp)
    rig.tick_handler = HandlerClaudeTokenRotation(
        pool=rig.pool,
        token_file=rig.token_file,
        marks_source=rig.source,
        overlay=overlay().model_copy(
            update={
                "spread": ModelRotationSpread(
                    enabled=enabled, in_use_longer_than_seconds=BOUND_SECONDS
                )
            }
        ),
        host=rig.host,
        events_path=rig.events,
        now=rig.clock,
    )
    return rig


def age_login(rig: Rig) -> None:
    """Stand the login file at T0 - 2h: older than the bound, against the rig's clock that starts at T0."""
    epoch = (T0 - timedelta(seconds=AGE_SECONDS)).timestamp()
    os.utime(rig.token_path, (epoch, epoch))


def setup_spreadable(rig: Rig) -> None:
    """A usable login a lane has used, stood on the host longer than the spread bound."""
    rig.login("acct-a")
    rig.pool.handle(ModelClaudeTokenPoolRequest(operation="record_use", label="acct-a"))
    age_login(rig)


def test_an_aged_used_login_spreads_to_an_unused_token(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    setup_spreadable(rig)
    result = rig.tick()
    assert result.outcome == "spread"
    assert result.from_label == "acct-a"
    assert result.to_label in ("acct-b", "caia", "onexbot")
    assert rig.in_use() == result.to_label
    assert rig.token_file.fingerprint() == fingerprint(fake_token(result.to_label))
    rows = [
        ModelRotationEvent.model_validate_json(line)
        for line in rig.events.read_text().splitlines()
    ]
    assert [r.kind for r in rows] == ["spread"]
    assert rows[0].reason == "spread"
    assert rows[0].host == "h101"
    assert rows[0].from_label == "acct-a"
    assert rows[0].to_label == result.to_label
    # no token in the result or the journal: labels and fingerprints only
    assert fake_token(result.to_label) not in result.model_dump_json()
    assert fake_token(result.to_label) not in rig.events.read_text()
    # the previous login is kept, for a rollback
    assert rig.token_path.with_name(
        rig.token_path.name + ".prev"
    ).read_text().strip() == fake_token("acct-a")


def test_spread_skips_a_token_a_lane_has_used(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    rig.login("acct-a")
    for label in ("acct-a", "caia"):
        rig.pool.handle(
            ModelClaudeTokenPoolRequest(operation="record_use", label=label)
        )
    age_login(rig)
    result = rig.tick()
    assert result.outcome == "spread"
    # the two unused tokens are drawn, never the one a lane already used
    assert result.to_label in ("acct-b", "onexbot")
    assert result.to_label not in ("acct-a", "caia")
    assert rig.in_use() == result.to_label


def test_a_dry_run_spread_plans_without_writing(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    setup_spreadable(rig)
    before = rig.token_path.read_bytes()
    result = rig.tick_handler.handle(
        ModelClaudeTokenRotationRequest(host=rig.host, dry_run=True)
    )
    assert result.outcome == "planned"
    assert result.to_label in ("acct-b", "caia", "onexbot")
    assert not rig.events.exists()
    assert rig.token_path.read_bytes() == before


def test_a_disabled_spread_rule_keeps_the_login(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path, enabled=False)
    setup_spreadable(rig)
    before = rig.token_path.stat().st_mtime_ns
    result = rig.tick()
    assert result.outcome == "kept"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.stat().st_mtime_ns == before
    assert not rig.events.exists()


def test_an_unset_spread_rule_keeps_the_login(tmp_path: Path) -> None:
    rig = Rig(tmp_path)  # the default overlay declares no spread rule
    setup_spreadable(rig)
    before = rig.token_path.stat().st_mtime_ns
    result = rig.tick()
    assert result.outcome == "kept"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.stat().st_mtime_ns == before


def test_a_login_younger_than_the_bound_is_kept(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    rig.login("acct-a")
    rig.pool.handle(ModelClaudeTokenPoolRequest(operation="record_use", label="acct-a"))
    # the file was just written, so it stands younger than the bound even against the rig's clock
    before = rig.token_path.stat().st_mtime_ns
    result = rig.tick()
    assert result.outcome == "kept"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.stat().st_mtime_ns == before


def test_an_unused_login_is_kept_even_when_aged(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    rig.login("acct-a")
    age_login(rig)
    before = rig.token_path.stat().st_mtime_ns
    result = rig.tick()
    assert result.outcome == "kept"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.stat().st_mtime_ns == before


def test_a_spread_never_moves_onto_a_limited_unused_token(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    setup_spreadable(rig)
    for label in ("acct-b", "caia", "onexbot"):
        rig.limit(label)
    before = rig.token_path.stat().st_mtime_ns
    result = rig.tick()
    assert result.outcome == "kept"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.stat().st_mtime_ns == before


def test_a_spread_never_moves_onto_a_revoked_unused_token(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    setup_spreadable(rig)
    for label in ("acct-b", "caia", "onexbot"):
        rig.revoke(label)
    before = rig.token_path.stat().st_mtime_ns
    result = rig.tick()
    assert result.outcome == "kept"
    assert rig.in_use() == "acct-a"
    assert rig.token_path.stat().st_mtime_ns == before


def test_record_use_refuses_a_label_not_in_the_pool(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with pytest.raises(ClaudeTokenPoolRefusedError, match="ghost"):
        rig.pool.handle(
            ModelClaudeTokenPoolRequest(operation="record_use", label="ghost")
        )


def test_control_spread_disabled_leaves_the_aged_login(tmp_path: Path) -> None:
    # injected break: the spread rule is off (enabled=False), so the aged, used login stays on
    # acct-a. If the spread step is removed from the tick, the outcome assertion of
    # test_an_aged_used_login_spreads_to_an_unused_token turns red.
    rig = spread_rig(tmp_path, enabled=False)
    setup_spreadable(rig)
    result = rig.tick()
    assert result.outcome == "kept"
    assert rig.in_use() == "acct-a"
