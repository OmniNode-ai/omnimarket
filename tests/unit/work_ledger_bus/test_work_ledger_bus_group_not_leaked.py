# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""N append calls leave no ``work_ledger_append_client`` consumer group behind.

The append client waits for the terminal naming its own command envelope on
topics every caller shares. A stable group would split those partitions among
concurrent callers and hand a terminal to the wrong one, so each client keeps a
group of its own and deletes it in ``stop()``.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller
from tests.helpers.fake_group_broker import FakeGroupBroker, install_fake_admin

pytestmark = pytest.mark.unit

CALLS = 5


def test_work_ledger_bus_group_not_leaked(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = FakeGroupBroker()
    install_fake_admin(monkeypatch, broker)

    async def scenario() -> None:
        for _ in range(CALLS):
            caller = WorkLedgerAppendCaller(cast(ProtocolLabRunBus, broker))
            await caller.start()
            await caller.stop()

    asyncio.run(scenario())

    assert broker.live == set()
    assert broker.empty_groups == set()


def test_work_ledger_bus_group_is_unique_per_client() -> None:
    """Falsifies the stable-group alternative: concurrent clients must not share."""
    broker = FakeGroupBroker()
    first = WorkLedgerAppendCaller(cast(ProtocolLabRunBus, broker))
    second = WorkLedgerAppendCaller(cast(ProtocolLabRunBus, broker))

    async def scenario() -> None:
        await first.start()
        await second.start()
        assert len(broker.live) == 2

    asyncio.run(scenario())
