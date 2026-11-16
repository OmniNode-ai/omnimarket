# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24174: the plain-words helper drops an id-shaped token before it reads underscores as spaces."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_eval_board_compute.handlers import HandlerEvalBoard
from omnimarket.nodes.node_eval_board_compute.handlers.board_text import plain
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
)
from tests.nodes.node_eval_board_compute.support import (
    FIRST_TEST,
    LOCAL_ID,
    model,
    request,
    run,
)

pytestmark = pytest.mark.unit


def test_board_text_plain_reads_underscores_as_spaces() -> None:
    assert plain("registration_mismatch") == "registration mismatch"


def test_board_text_plain_drops_a_node_name_before_underscores_become_spaces() -> None:
    assert (
        plain("node_projection_eval_results registration_mismatch")
        == "registration mismatch"
    )
    assert plain("node_only") == ""


def test_board_text_a_void_note_carries_no_node_name() -> None:
    record = HandlerEvalBoard().handle(
        request(
            model(
                LOCAL_ID,
                (
                    run(
                        "r1",
                        FIRST_TEST,
                        state=EnumEvalRunState.VOID,
                        void_reason="node_code_eval_trial_reducer registration_mismatch",
                    ),
                ),
            )
        )
    )
    (note,) = record.footer.void_notes
    assert note.endswith("was not counted: registration mismatch.")
    assert "node" not in note
