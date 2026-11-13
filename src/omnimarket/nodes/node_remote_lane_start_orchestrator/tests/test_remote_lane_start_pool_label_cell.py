# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The CLAIM row the lane start writes carries the pool label the lane starts under (OMN-23550).

Shape of ``test_remote_lane_start_observe.py::test_act_control_makes_the_calls_observe_does_not``: the same ``Rig`` over the
scripted transport and a ledger fixture file. The seam is the CLAIM row's cells: ``login=`` (read by the token pool node's
steering handler) and ``pool_label=`` (the cell this change adds) are written together, from the one label the draw found.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_spi.contracts.services import ModelLaneHostRef

from omnimarket.models.engine_login import ModelEngineLoginDraw
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_pool_steering import (
    count_open_lanes,
)
from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    T0,
    Rig,
    cells,
    request,
    settings,
)

pytestmark = pytest.mark.unit


class _RefusingLoginDraw:
    """A draw the pool refuses at the lanes-per-label cap (the error chain's input)."""

    synchronous_execution = True

    def draw_login(self, host: ModelLaneHostRef) -> ModelEngineLoginDraw:
        return ModelEngineLoginDraw(
            state="refused", detail="at cap", refusal="label_lane_cap"
        )


def test_golden_claim_carries_pool_label_and_login(tmp_path: Path) -> None:
    """Golden chain: a drawn label rides on the CLAIM as ``pool_label=`` and ``login=``, and on the receipt."""
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(),
        cfg=settings(),
        login_labels=("example-label",),
    )
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    [claim] = rig.ledger_rows("CLAIM")
    got = cells(claim)
    assert got["pool_label"] == "example-label"
    assert got["login"] == "example-label"
    assert result.receipt.login == "example-label"


def test_pool_that_draws_nothing_writes_pool_label_none(tmp_path: Path) -> None:
    """A pool with no usable token still names it: ``pool_label=none``, never a missing cell."""
    rig = Rig(
        tmp_path, ModelScriptedTransportScript(), cfg=settings(), login_labels=(None,)
    )
    rig.orchestrator.run(request())
    [claim] = rig.ledger_rows("CLAIM")
    assert cells(claim)["pool_label"] == "none"


def test_control_no_draw_wired_writes_neither_cell(tmp_path: Path) -> None:
    """Failing control: with no draw wired the CLAIM has no label cell, so the golden test reads the draw, not a default."""
    rig = Rig(tmp_path, ModelScriptedTransportScript(), cfg=settings())
    rig.orchestrator.run(request())
    [claim] = rig.ledger_rows("CLAIM")
    got = cells(claim)
    assert "pool_label" not in got
    assert "login" not in got


def test_error_chain_refused_draw_writes_no_claim(tmp_path: Path) -> None:
    """Error chain: the pool refuses the start, so no CLAIM row exists to carry a label."""
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(),
        cfg=settings(),
        login_labels=("example-label",),
    )
    vars(rig.orchestrator)["_login"] = _RefusingLoginDraw()
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.LOGIN_REFUSED
    assert rig.ledger_rows("CLAIM") == []


def test_seam_the_row_written_is_the_row_the_pool_steering_counts(
    tmp_path: Path,
) -> None:
    """Seam (writer: this node; reader: the pool node's steering count): both cells name the label the reader counts.

    Fails when either side changes shape: the writer dropping ``login=`` or renaming the label, or the reader's
    cell name moving off ``login=``.
    """
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(),
        cfg=settings(),
        login_labels=("example-label",),
    )
    rig.orchestrator.run(request())
    [claim] = rig.ledger_rows("CLAIM")
    got = cells(claim)
    counted = count_open_lanes(claim, now=T0, max_age_seconds=3600)
    assert [(c.label, c.lanes) for c in counted] == [("example-label", 1)]
    assert got["pool_label"] == counted[0].label
