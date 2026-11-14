# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of node_lease_receipt_reducer, in the place the coverage gate reads (OMN-24484).

The chains themselves, with their bus legs, controls and the Redpanda profile, are in tests/chain
(lease_chain.py and its two test files). This file runs one golden chain and one error chain of them
on the in-memory bus and reads the events: an ``acquire`` returns a recorded event and a stale-epoch
receipt returns a refused event carrying ``stale-epoch``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnimarket.nodes.node_lease_receipt_reducer.models.enum_lease import (
    EnumLeaseStatus,
)
from tests.chain.lease_chain import ModelLeaseChainOverlay
from tests.chain.test_lease_receipt_error import drive_stale
from tests.chain.test_lease_receipt_golden import drive_golden

pytestmark = pytest.mark.unit


def test_golden_chain_event_acquire_returns_a_recorded_event(tmp_path: Path) -> None:
    results = asyncio.run(drive_golden(ModelLeaseChainOverlay(), tmp_path))

    assert results[0].status is EnumLeaseStatus.RECORDED
    assert results[0].epoch == 1


def test_error_chain_event_stale_epoch_receipt_returns_a_refused_event() -> None:
    # drive_stale reads the refused reply itself: status refused, reason stale-epoch, the input
    # table unchanged, and the reply on the refused topic of the contract.
    asyncio.run(drive_stale(ModelLeaseChainOverlay()))
