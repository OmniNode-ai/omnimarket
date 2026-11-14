# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""No setting of the lab-fill judge is spelled in its code: every one rides on the overlay-loaded policy (OMN-24109)."""

from __future__ import annotations

from importlib.resources import files

import pytest

pytestmark = pytest.mark.unit

LITERALS = (
    "WORKFLOW_RESULT_TRUNCATED",
    "workflow-result-truncated",
    "lab-fill-blind",
    "lab-fill-no-result",
    "lab-fill-claude-refused",
    "hit your",
    "linear-snapshot-absent",
    "LAB FILL REFUSED",
    "stoppage:lab-idle",
    "lab_fill_idle",
)


@pytest.mark.parametrize("literal", LITERALS)
def test_no_hard_coded_values_in_the_handler(literal: str) -> None:
    source = (
        files("omnimarket.nodes.node_lab_fill_fire_outcome_compute.handlers")
        .joinpath("handler_lab_fill_fire_outcome.py")
        .read_text(encoding="utf-8")
    )
    assert literal not in source
