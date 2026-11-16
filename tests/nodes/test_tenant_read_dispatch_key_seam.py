# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the event type a bus caller stamps on a tenant read command reaches the bus entry's dispatcher.

The runtime indexes a handler entry's dispatcher under its event model's class name, every literal
subscribe topic and the entry's explicit ``event_type`` (which replaces the topic-derived alias). The
caller (ScmBusCaller) stamps ``<producer>.<event>`` of the command topic. If the contract's explicit
``event_type`` is anything else the runtime answers every command with a boundary failure terminal
(``no_dispatcher``) that carries no ``reason`` and no ``detail``, which is how scenario R8 failed on
the dev runtime. This test drives the runtime's own key derivation against the real contracts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    derive_entry_message_types,
)
from omnibase_infra.runtime.auto_wiring.models import ModelDiscoveredContract
from omnibase_infra.runtime.auto_wiring.models.model_handler_routing_entry import (
    ModelHandlerRoutingEntry,
)

from omnimarket.delegated_test_loop.lab_run_bus import event_type_for
from tests.nodes.tenant_read_support import NODES_ROOT

pytestmark = pytest.mark.unit

NODES = ("node_tenant_savings_read_effect", "node_tenant_delegations_read_effect")


def _command_topic(node: str) -> str:
    raw = yaml.safe_load((NODES_ROOT / node / "contract.yaml").read_text())
    return str(raw["runtime_dispatch"]["command_topic"])


def _discovered(node: str) -> ModelDiscoveredContract:
    return _parse_contract(
        contract_path=Path(NODES_ROOT / node / "contract.yaml"),
        entry_point_name=node,
        package_name="omnimarket",
        package_version="0.0.0",
    )


def _bus_entry(
    contract: ModelDiscoveredContract, topic: str
) -> ModelHandlerRoutingEntry:
    assert contract.handler_routing is not None
    owners = [h for h in contract.handler_routing.handlers if h.topic == topic]
    assert len(owners) == 1, [h.operation for h in owners]
    return owners[0]


@pytest.mark.parametrize("node", NODES)
def test_the_command_topics_wire_event_type_reaches_the_bus_entrys_dispatcher(
    node: str,
) -> None:
    topic = _command_topic(node)
    contract = _discovered(node)
    keys = derive_entry_message_types(contract, _bus_entry(contract, topic))
    assert keys is not None
    assert event_type_for(topic) in keys, (event_type_for(topic), sorted(keys))


@pytest.mark.parametrize("node", NODES)
def test_the_bus_entrys_event_model_is_also_a_dispatcher_key(node: str) -> None:
    contract = _discovered(node)
    entry = _bus_entry(contract, _command_topic(node))
    assert entry.event_model is not None
    keys = derive_entry_message_types(contract, entry)
    assert keys is not None
    assert entry.event_model.name in keys


@pytest.mark.parametrize("node", NODES)
def test_failing_control_an_operation_style_event_type_misses_the_wire_key(
    node: str,
) -> None:
    """The old shape (an explicit event_type that is not the wire alias) leaves the caller's key unmatched."""
    topic = _command_topic(node)
    contract = _discovered(node)
    old_shape = _bus_entry(contract, topic).model_copy(
        update={"event_type": "tenant.read_bus"}
    )
    keys = derive_entry_message_types(contract, old_shape)
    assert keys is not None
    assert event_type_for(topic) not in keys


@pytest.mark.parametrize("node", NODES)
def test_the_http_entry_keeps_its_operation_style_event_type(node: str) -> None:
    contract = _discovered(node)
    assert contract.handler_routing is not None
    http = [h for h in contract.handler_routing.handlers if not h.topic]
    assert len(http) == 1
    assert http[0].event_type == http[0].operation
