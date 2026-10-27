# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TC.4 contract dispatch has identical results on both local bus bindings."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.event_bus.event_bus_semantic_fake import EventBusSemanticFake
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback

from omnimarket.nodes.node_context_source_ledger_effect import (
    NodeContextSourceLedgerEffect,
)
from omnimarket.nodes.node_context_source_ledger_effect.models import (
    ModelLedgerSourceResult,
)
from tests.test_golden_chain_context_source_ledger_effect import (
    CONTRACT,
    D1_CORRECTION,
    D1_RULING,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "bus_class",
    [EventBusInmemory, EventBusSemanticFake],
    ids=["inmemory", "broker-semantics"],
)
@pytest.mark.parametrize("scenario", ["resolved", "missing", "escape", "denied"])
def test_pinned_source_contract_dispatch_on_bus_bindings(
    tmp_path: Path,
    bus_class: type[EventBusInmemory] | type[EventBusSemanticFake],
    scenario: str,
) -> None:
    root = tmp_path / "manifest"
    root.mkdir()
    (root / "ledger.md").write_text(D1_RULING + "\n" + D1_CORRECTION + "\n")
    (tmp_path / "outside.md").write_text(D1_RULING + "\n")
    contract = yaml.safe_load(CONTRACT.read_text())
    source = contract["context_source_ledger"]["ledger_source"]
    source.update(
        mode="pinned",
        manifest_root=str(root),
        ledger_paths=["ledger.md"],
        tenant_ids=["fixture"],
    )
    if scenario == "missing":
        source["ledger_paths"] = ["missing.md"]
    elif scenario == "escape":
        # A valid prefix must never leak out as a resolved partial snapshot.
        source["ledger_paths"].append("../outside.md")
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract))
    manifest = discover_contracts_from_paths([path])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    node = NodeContextSourceLedgerEffect(contract_path=path)
    dispatch = _make_dispatch_callback(node, event_model=route.event_model)
    request = ModelContextSourceRequest(
        factor=EnumContextFactor.OPERATOR_RULINGS,
        revision="2026-10-02T12:00:00Z",
        access=ModelContextAccess(
            tenant_id="other" if scenario == "denied" else "fixture"
        ),
    )
    command_topic = contract["runtime_dispatch"]["command_topic"]
    terminals: list[ModelLedgerSourceResult] = []

    async def run() -> None:
        bus = bus_class()

        async def on_command(message: ModelEventMessage) -> None:
            result = await dispatch(
                {
                    "payload": json.loads(message.value),
                    "correlation_id": "00000000-0000-4000-8000-000000021127",
                }
            )
            assert result is not None
            await bus.publish(
                node.terminal_event,
                key=None,
                value=ModelLedgerSourceResult.model_validate(result.output_events[0])
                .model_dump_json()
                .encode(),
            )

        async def on_terminal(message: ModelEventMessage) -> None:
            terminals.append(ModelLedgerSourceResult.model_validate_json(message.value))

        await bus.start()
        try:
            await bus.subscribe(
                node.terminal_event,
                on_message=on_terminal,
                group_id="ledger-source-result",
            )
            await bus.subscribe(
                command_topic, on_message=on_command, group_id="ledger-source-command"
            )
            await bus.publish(
                command_topic, key=None, value=request.model_dump_json().encode()
            )
        finally:
            await bus.close()

    asyncio.run(run())
    assert len(terminals) == 1
    result = terminals[0]
    expected = "unavailable" if scenario in {"missing", "escape"} else scenario
    assert result.status.value == expected
    if scenario == "resolved":
        (entry,) = result.entries
        assert entry.text == D1_RULING
        assert entry.supersession_status.value == "corrected"
        assert [(c.text, c.row_stamp) for c in entry.corrected_by] == [
            (D1_CORRECTION, D1_CORRECTION[:20])
        ]
    else:
        assert not result.entries
