# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The required-span rule for prose items is the loss node's rule, pinned against it."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_compaction_ground_truth_compute.handlers.required_spans import (
    content_spans,
)
from omnimarket.nodes.node_compaction_loss_compute.handlers import (
    handler_compaction_loss as loss,
)

pytestmark = pytest.mark.unit

SENTENCES = (
    "I will send the vendor the corrected invoice dispute summary tomorrow",
    "the report waiting for acknowledgement from everyone involved",
    "ok then",
    "Never deploy anything on staging without approval from operations",
)


@pytest.mark.parametrize("sentence", SENTENCES)
def test_content_spans_match_the_loss_node(sentence: str) -> None:
    assert content_spans(sentence) == loss._content_spans(sentence)
