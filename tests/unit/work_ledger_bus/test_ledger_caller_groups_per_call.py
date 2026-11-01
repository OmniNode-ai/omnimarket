# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21818 -- a work-ledger append joins no consumer group.

``WorkLedgerAppendCaller`` built one ``work_ledger_append_client`` group per
append and read the completed and failed terminals in it: 12,907 Empty groups
sat on the dev broker, two thirds of the 19,154 that crashed it three times on
2026-10-31. The caller now reads both terminals with no group, so only the
ledger host's own groups exist however many appends are made.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnimarket.nodes.node_work_ledger_append_effect import (
    EnumWorkLedgerAppendStatus,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller, WorkLedgerAppendHost
from tests.unit.work_ledger_bus.test_work_ledger_append_bus import (
    _Bus,
    _Handler,
    _request,
)

pytestmark = pytest.mark.unit

CALLS = 100
#: Groups a hundred appends may create beyond the ledger host's own.
MAX_CALLER_GROUPS = 1


def test_groups_per_call_a_hundred_appends_create_at_most_one_group() -> None:
    async def scenario() -> tuple[set[str], set[str], list[EnumWorkLedgerAppendStatus]]:
        bus = _Bus()
        await bus.start()
        host = WorkLedgerAppendHost(
            bus,
            _Handler(),
            writer_principals=["developer"],
            operator_principal="operator",
        )
        await host.start()
        host_groups = {group for _, group in bus.groups}
        statuses: list[EnumWorkLedgerAppendStatus] = []
        # One caller per append, as `onex work-ledger append` builds them.
        for _ in range(CALLS):
            caller = WorkLedgerAppendCaller(bus, principal="developer")
            statuses.append((await caller.append(_request(), timeout_s=5)).status)
            await caller.stop()
        all_groups = {group for _, group in bus.groups}
        await host.stop()
        await bus.close()
        return host_groups, all_groups, statuses

    host_groups, all_groups, statuses = asyncio.run(scenario())

    assert statuses == [EnumWorkLedgerAppendStatus.ACCEPTED] * CALLS
    assert len(all_groups - host_groups) <= MAX_CALLER_GROUPS
    assert not any("work_ledger_append_client" in group for group in all_groups)


def test_groups_per_call_the_caller_source_mints_no_group_name() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "src/omnimarket/work_ledger_bus/bus.py"
    ).read_text(encoding="utf-8")
    caller = source[
        source.index("class WorkLedgerAppendCaller") : source.index(
            "def _error_receipt"
        )
    ]
    assert "group_id" not in caller
    assert "self._group" not in caller
    assert "work_ledger_append_client" not in caller
    assert "uuid4().hex" not in caller
