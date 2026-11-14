# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_host_disk_orchestrator (OMN-23866): reading in, scheduled run, command out, result in, run recorded.

The full chain, including its error legs and the dev broker half, is tests/chains/host_disk/test_chain_host_disk_workflow.py;
this is the node-local golden chain the coverage gate looks for, run on the in-memory bus.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_host_disk_orchestrator import EnumHostDiskRunOutcome
from tests.chains.host_disk.host_disk_chain import InMemoryWire, new_key, run_chain

from .orchestrator_inputs import HOSTS, Orchestrator

pytestmark = pytest.mark.unit


async def test_golden_chain_host_disk_orchestrator_in_memory() -> None:
    [record] = await run_chain(InMemoryWire(), Orchestrator(), new_key())
    assert record.outcome is EnumHostDiskRunOutcome.COMPLETED
    assert [r.host_name for r in record.readings] == list(HOSTS)
    assert [r.status for r in record.results] == ["completed"] * len(HOSTS)
