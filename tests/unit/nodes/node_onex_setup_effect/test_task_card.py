# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-26620): each delegated task is a card with a coloured state bar
and one line for model, tokens, time and cost.

Read on dev (bf09f2968): a delegated answer is written into ``#output`` as
plain lines (see ``cli_tui`` delegation handling); there is no ``TaskCard``
widget and no ``task_card`` section in ``tui_design.yaml``. The approved
04-task-card mockup renders a recorded answer as a card. Values (state-bar
colour token and the facts-line wording) must come from ``tui_design.yaml``
(OMN-23413 AC-27); AC8's literal scan separately proves none is in code.

Failure modes:
  AC3-F1  no TaskCard widget exists for a recorded answer.
  AC3-F2  the facts line wraps into more than one line, or its wording is not
          a design token.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from omnimarket.nodes.node_onex_setup_effect import cli_tui
from omnimarket.nodes.node_onex_setup_effect.models.model_tui_design import tui_design

pytestmark = pytest.mark.unit

#: A recorded delegate answer, with the four facts the card must show.
_RECORDED = {
    "model": "Qwen3.8-27B",
    "tokens_in": 1200,
    "tokens_out": 340,
    "seconds": 2.4,
    "cost": 0.0042,
    "state": "done",
}


def test_task_card_renders() -> None:
    """AC3-F1: a recorded answer becomes a TaskCard with a coloured state bar."""
    card_cls = getattr(cli_tui, "TaskCard", None)
    assert card_cls is not None, (
        "AC3: cli_tui has no TaskCard widget (04-task-card mockup approved)"
    )
    card = card_cls.from_recorded(_RECORDED)
    console = Console(width=140, force_terminal=False, color_system=None)
    with console.capture() as capture:
        console.print(card.render())
    text = capture.get()
    assert "Qwen3.8-27B" in text, "card omits the model"
    assert "done" in text.lower() or "✓" in text, "card omits the state bar"
    # The state-bar colour is a theme token declared in the design file.
    design = tui_design()
    assert design.task_card.state_bar_colour in type(design.theme).model_fields, (
        "state-bar colour is not a theme token from tui_design.yaml (AC27)"
    )


def test_card_facts_line_single() -> None:
    """AC3-F2: model, tokens, time and cost fit on ONE line, from design tokens."""
    design = tui_design()
    assert hasattr(design, "task_card"), (
        "AC3/AC27: tui_design.yaml has no task_card.* keys"
    )
    card_cls = getattr(cli_tui, "TaskCard", None)
    assert card_cls is not None, "AC3: no TaskCard widget"
    card = card_cls.from_recorded(_RECORDED)
    facts = card.facts_line()
    assert "\n" not in facts, "facts line must be a single line"
    # Tokens are the in and out counts together, in thousands (mockups-v2: "1.5k tokens").
    for word in ("Qwen3.8-27B", "1.5k tokens", "2.4 s", "$0.0042"):
        assert word in facts, (word, facts)
    for raw in ("1200", "340"):
        assert raw not in facts, (raw, facts)
    # The wording is re-used from the design file, not written in code (AC27).
    assert design.task_card.facts_template, "no facts wording token in design"
