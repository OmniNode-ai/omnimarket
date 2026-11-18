# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's folded record leaves the runtime as the state-updated event (OMN-25040).

The branch deploy orchestrator and the host install orchestrator both wait on the state-updated topic for the record the
reducer folded. A reducer that returns only projections never publishes it, so each orchestrator stalled after its first
step on the dev runtime. The runtime publishes a reducer's bare model return as an event only when the contract's
``published_events`` names the model; these tests run the runtime's own result normalisation over the handler's real output
and the contract's real map.

* golden: the handler's output normalises to one output event, the record, on the state-updated topic;
* control: the same handler output normalised with no published events declared is a projection and no event, so the golden
  assertion is not vacuous.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from omnibase_core.enums import EnumNodeKind
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _normalize_handler_result,
)
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)

from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
    HandlerHostInstallState,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallState,
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
)
from tests.nodes.node_host_install_state_reducer.support import (
    NODE_DIR,
    RUN_ID,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

STATE_TOPIC = "onex.evt.omnimarket.host-install-state-updated.v1"
MESSAGE_TYPE = "HostInstallReduceCommand"


def published_events() -> dict[str, str]:
    return load_published_events_map(NODE_DIR / "contract.yaml")


def normalised(names: frozenset[str] | None) -> Any:
    handler = HandlerHostInstallState(overlay())
    output = asyncio.run(
        handler.handle(ModelHostInstallReduceCommand(prior=None, event=request()))
    )
    return _normalize_handler_result(
        output,
        {"correlation_id": str(RUN_ID)},
        MESSAGE_TYPE,
        handler_node_kind=EnumNodeKind.REDUCER,
        published_event_names=names,
    )


def test_contract_declares_the_record_as_the_state_updated_event() -> None:
    assert published_events() == {"HostInstallRecord": STATE_TOPIC}


def test_the_folded_record_is_published_as_the_state_updated_event() -> None:
    result = normalised(frozenset(published_events()))
    (published,) = result.output_events
    assert isinstance(published, ModelHostInstallRecord)
    assert published.state is EnumHostInstallState.REQUESTED
    assert published.correlation_id == RUN_ID
    assert result.projection_intents == ()


def test_control_without_a_published_events_map_the_record_is_never_published() -> None:
    result = normalised(None)
    assert result.output_events == []
    assert len(result.projection_intents) == 1
