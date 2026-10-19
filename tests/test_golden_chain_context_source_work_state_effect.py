# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the work-state context source (OMN-21111).

A ``ModelContextSourceRequest`` command reaches the node through the runtime's
typed dispatch and the terminal event carries the session's unanswered handoff
message whole, without the message an ACK answered.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    ModelLedgerSourceRow,
)
from omnimarket.nodes.node_context_source_work_state_effect import (
    NodeContextSourceWorkStateEffect,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_context_source_work_state_effect/contract.yaml"
)
HANDOFF = "2026-10-19T07:00:00Z | MSG | from=orch-a | id=2026-10-19T07:00:00Z-orch-a | to=orch-a | ticket=OMN-1 | handoff: finish the read path"
ANSWERED = "2026-10-19T07:05:00Z | MSG | from=orch-a | id=2026-10-19T07:05:00Z-orch-a | to=orch-a | ticket=OMN-1 | handoff: answered"
ACK = "2026-10-19T07:06:00Z | ACK | lane=orch-a | re=2026-10-19T07:05:00Z-orch-a | ticket=OMN-1 | done"


class _Reader:
    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        return tuple(
            ModelLedgerSourceRow(row_id=f"r{i}", raw_row=raw, source_ref=f"ledger:{i}")
            for i, raw in enumerate((HANDOFF, ANSWERED, ACK))
        )


def _node() -> NodeContextSourceWorkStateEffect:
    return NodeContextSourceWorkStateEffect(reader=_Reader(), tenant_ids=("lab",))


def test_context_source_work_state_golden_chain_contract_topics() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    handler = _node()
    assert handler.terminal_event == contract["terminal_event"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert handler.terminal_event in contract["event_bus"]["publish_topics"]
    route = contract["handler_routing"]["handlers"][0]
    assert route["event_model"].endswith("ModelContextSourceRequest")
    assert contract["output_model"].endswith("ModelContextSourceResult")


def test_context_source_work_state_golden_chain_real_runtime_typed_dispatch() -> None:
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    callback = _make_dispatch_callback(_node(), event_model=route.event_model)
    request = ModelContextSourceRequest(
        factor=EnumContextFactor.OPEN_WORK,
        caller_scope=ModelContextCallerScope(lane="orch-a"),
        access=ModelContextAccess(tenant_id="lab"),
        revision="2026-10-19T08:00:00Z",
    )
    dispatch = asyncio.run(
        callback(
            {
                "payload": request.model_dump(mode="json"),
                "correlation_id": "00000000-0000-4000-8000-000000021111",
            }
        )
    )
    assert dispatch is not None
    terminal = ModelContextSourceResult.model_validate(dispatch.output_events[0])
    assert terminal.status is EnumContextSourceStatus.RESOLVED
    assert [entry.text for entry in terminal.entries] == [HANDOFF]
