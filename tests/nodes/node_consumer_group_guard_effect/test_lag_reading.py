# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lag reading of node_consumer_group_guard_effect: end offset minus committed offset per partition (OMN-23992)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_consumer_group_guard import (
    sum_lag,
)

pytestmark = pytest.mark.unit


def test_lag_is_the_end_offset_minus_the_committed_offset_over_partitions() -> None:
    committed = {("t", 0): 90, ("t", 1): 10}
    marks = {("t", 0): (0, 100), ("t", 1): (0, 15)}
    assert sum_lag(committed, marks) == 15


def test_a_group_caught_up_reads_zero() -> None:
    assert sum_lag({("t", 0): 100}, {("t", 0): (0, 100)}) == 0


def test_a_partition_never_committed_counts_every_message_it_still_holds() -> None:
    assert sum_lag({("t", 0): -1001}, {("t", 0): (40, 100)}) == 60


def test_a_committed_offset_past_the_end_never_reads_negative() -> None:
    assert sum_lag({("t", 0): 120}, {("t", 0): (0, 100)}) == 0


def test_a_partition_with_no_watermark_is_an_error_not_a_zero() -> None:
    with pytest.raises(RuntimeError):
        sum_lag({("t", 0): 5}, {})
