# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The spread and the assign act on the pool's draw order, which a new lane reads (OMN-23550).

A lane does not read the host's login file: the pool's ``resolve`` hands it the first usable token in
the draw order, and the rotation tick resets that order to the sorted labels shifted by the host's
rank. Measured 2026-11-10 15:11Z: a login swapped by hand was never drawn. These tests run the tick's
reset and then ``resolve``, the order a lane start takes, on the real pool over a temporary store.
The failing controls are the release (the tick's own order stands again) and the refusals (nothing
is pinned). Every token is fake; labels only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    ClaudeTokenPoolRefusedError,
)
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_rotation_tick import (
    HandlerClaudeTokenAssign,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolRequest,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelClaudeTokenAssignRequest,
    ModelClaudeTokenAssignResult,
)
from omnimarket.nodes.node_claude_token_pool_effect.runtime_claude_token_pool import (
    main,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_ssh_transport import (
    _DRAWN_LABEL,
)
from tests.nodes.node_claude_token_pool_effect.test_claude_token_rotation_tick import (
    Rig,
    fake_token,
)
from tests.nodes.node_claude_token_pool_effect.test_claude_token_spread import (
    age_login,
    setup_spreadable,
    spread_rig,
)

pytestmark = pytest.mark.unit


def assign(rig: Rig, label: str) -> ModelClaudeTokenAssignResult:
    return HandlerClaudeTokenAssign(
        pool=rig.pool,
        token_file=rig.token_file,
        host=rig.host,
        events_path=rig.events,
        now=rig.clock,
    ).handle(ModelClaudeTokenAssignRequest(host=rig.host, label=label))


def pool_op(rig: Rig, operation: str, label: str | None = None, **extra: object):
    return rig.pool.handle(
        ModelClaudeTokenPoolRequest(operation=operation, label=label, **extra)
    )


def drawn(rig: Rig) -> str | None:
    """What a lane start draws: the pool's own resolve."""
    return pool_op(rig, "resolve").chosen


def test_reorder_keeps_a_pinned_label_first(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pool_op(rig, "pin", "caia")
    done = pool_op(rig, "reorder", labels=("acct-a", "acct-b", "caia", "onexbot"))
    assert done.tokens[0].label == "caia"
    assert done.pinned == "caia"


def test_assign_pins_the_label_through_the_ticks_reset(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    base_head = rig.tick().order[0]
    assert base_head != "onexbot"
    result = assign(rig, "onexbot")
    assert result.outcome == "assigned"
    assert result.pinned is True
    ticked = rig.tick()
    assert ticked.order[0] == "onexbot"
    assert drawn(rig) == "onexbot"
    assert rig.in_use() == "onexbot"


def test_failing_control_release_returns_the_draw_to_the_ticks_order(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    assign(rig, "onexbot")
    assert drawn(rig) == "onexbot"
    released = pool_op(rig, "release")
    assert released.pinned is None
    ticked = rig.tick()
    assert drawn(rig) == ticked.order[0] != "onexbot"


def test_an_assign_of_the_login_already_in_use_still_pins(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("onexbot")
    result = assign(rig, "onexbot")
    assert result.outcome == "unchanged"
    assert result.pinned is True
    rig.tick()
    assert drawn(rig) == "onexbot"


def test_spread_survives_the_ticks_reset(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    setup_spreadable(rig)
    first = rig.tick()
    assert first.outcome == "spread"
    target = first.to_label
    assert target is not None
    pooled = pool_op(rig, "status")
    assert (pooled.pinned, pooled.pinned_by) == (target, "spread")
    again = rig.tick()
    assert again.order[0] == target
    assert drawn(rig) == target


def test_spread_never_moves_off_an_assign_pin(tmp_path: Path) -> None:
    rig = spread_rig(tmp_path)
    setup_spreadable(rig)
    assign(rig, "acct-b")
    pool_op(rig, "record_use", "acct-b")
    age_login(rig)
    result = rig.tick()
    assert result.outcome == "kept"
    assert drawn(rig) == "acct-b"
    assert rig.in_use() == "acct-b"


def test_resolve_counts_the_use_of_the_token_it_hands_out(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    label = drawn(rig)
    status = pool_op(rig, "status")
    assert {t.label: t.uses for t in status.tokens}[label or ""] == 1
    assert sum(t.uses for t in status.tokens) == 1


def test_pin_refuses_a_limited_revoked_or_unknown_label(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.limit("acct-b")
    rig.revoke("caia")
    for label in ("acct-b", "caia", "nope"):
        with pytest.raises(ClaudeTokenPoolRefusedError):
            pool_op(rig, "pin", label)
    assert pool_op(rig, "status").pinned is None


def test_a_refused_assign_pins_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.login("acct-a")
    rig.limit("acct-b")
    result = assign(rig, "acct-b")
    assert result.outcome == "refused"
    assert result.pinned is False
    assert pool_op(rig, "status").pinned is None
    assert rig.in_use() == "acct-a"
    assert fake_token("acct-b") not in result.model_dump_json()


def test_seam_the_pool_resolve_line_names_the_pinned_label_to_the_lane_start_reader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seam: the pool CLI's ``resolve`` stderr line is what the lane start's ssh draw reads for its label.

    The reader keeps the last stderr line and takes ``label=<label>`` from its end. If the pool's line
    changes shape, or the pin stops reaching the draw, the label the receipt records is wrong.
    """
    rig = Rig(tmp_path)
    rig.login("acct-a")
    assign(rig, "onexbot")
    rig.tick()
    assert main(["resolve"], handler=rig.pool) == 0
    last = capsys.readouterr().err.strip().splitlines()[-1]
    found = _DRAWN_LABEL.search(last)
    assert found is not None
    assert found.group(1) == "onexbot"
    assert fake_token("onexbot") not in last
