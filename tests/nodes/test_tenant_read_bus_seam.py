# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the bus topics each read contract names equal the dispatch block's topics."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryTenantProjectionReader,
)
from omnimarket.models.model_tenant_delegation_projection import (
    ModelDelegationRunRow,
    ModelTenantSavingsAggregate,
)
from omnimarket.models.model_tenant_read_bus import (
    EnumTenantReadKind,
    ModelTenantReadCommand,
    ModelTenantReadCompleted,
)
from omnimarket.nodes.node_tenant_delegations_read_effect.handlers.handler_tenant_delegations_read_bus import (
    HandlerTenantDelegationsReadBus,
)
from omnimarket.nodes.node_tenant_savings_read_effect.handlers.handler_tenant_savings_read_bus import (
    HandlerTenantSavingsReadBus,
)
from tests.nodes.tenant_read_support import TENANT_A, contract_of

pytestmark = pytest.mark.unit


def _load_contract(node: str) -> dict:
    return contract_of(node)


def _topics_from_contract(contract: dict) -> dict[str, str]:
    """Build the topic map the way load_ticket_topics builds it from runtime_dispatch."""
    rd = contract["runtime_dispatch"]
    return {
        "command": rd["command_topic"],
        "success": rd["terminal_events"]["success"],
        "failure": rd["terminal_events"]["failure"],
    }


def test_savings_contract_topics_match_dispatch() -> None:
    contract = _load_contract("node_tenant_savings_read_effect")
    topics = _topics_from_contract(contract)
    # The subscribe/publish topics must include the command and both terminals
    subs = contract["event_bus"]["subscribe_topics"]
    pubs = contract["event_bus"]["publish_topics"]
    assert topics["command"] in subs
    assert topics["success"] in pubs
    assert topics["failure"] in pubs


def test_delegations_contract_topics_match_dispatch() -> None:
    contract = _load_contract("node_tenant_delegations_read_effect")
    topics = _topics_from_contract(contract)
    subs = contract["event_bus"]["subscribe_topics"]
    pubs = contract["event_bus"]["publish_topics"]
    assert topics["command"] in subs
    assert topics["success"] in pubs
    assert topics["failure"] in pubs


async def test_savings_bus_body_carries_r8_totals_keys() -> None:
    """The savings terminal body carries every key the R8 contract's lane.totals_keys names."""
    needed = set(
        _load_contract("node_delegation_scenario_r8_effect")["lane"][
            "totals_keys"
        ].values()
    )
    reader = InMemoryTenantProjectionReader(
        savings={
            TENANT_A: ModelTenantSavingsAggregate(delegation_runs=1, total_tokens=100)
        }
    )
    answer = await HandlerTenantSavingsReadBus(reader=reader).handle(
        ModelTenantReadCommand(
            correlation_id=uuid4(), tenant_id=TENANT_A, kind=EnumTenantReadKind.SAVINGS
        )
    )
    assert isinstance(answer, ModelTenantReadCompleted)
    assert needed <= set(answer.body), needed - set(answer.body)


async def test_delegations_bus_body_runs_carry_r8_run_keys() -> None:
    """Each run in the delegations terminal body carries every key the R8 contract's lane.run_keys names."""
    lane = _load_contract("node_delegation_scenario_r8_effect")["lane"]
    needed = set(lane["run_keys"].values())
    reader = InMemoryTenantProjectionReader(
        runs={
            TENANT_A: (
                ModelDelegationRunRow(run_id="r1", tokens=50, data_source="experiment"),
            )
        }
    )
    answer = await HandlerTenantDelegationsReadBus(reader=reader).handle(
        ModelTenantReadCommand(
            correlation_id=uuid4(),
            tenant_id=TENANT_A,
            kind=EnumTenantReadKind.DELEGATIONS,
        )
    )
    assert isinstance(answer, ModelTenantReadCompleted)
    runs = answer.body[lane["runs_list_key"]]
    assert runs
    for run in runs:
        assert needed <= set(run), needed - set(run)
    assert runs[0]["traffic_class"] == "synthetic"
