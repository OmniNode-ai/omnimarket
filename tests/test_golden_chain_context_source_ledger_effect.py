# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the work-ledger context source (OMN-21127).

A ``ModelContextSourceRequest`` command reaches the node through the runtime's
typed dispatch and the terminal event carries the D1 ruling whole with its
correction in ``corrected_by``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_supersession_status import (
    EnumContextSupersessionStatus,
)
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)

from omnimarket.nodes.node_context_source_ledger_effect import (
    NodeContextSourceLedgerEffect,
)
from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    ModelLedgerSourceRow,
)
from omnimarket.nodes.node_context_source_ledger_effect.models import (
    ModelLedgerSourceResult,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    row_id_of,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_context_source_ledger_effect/contract.yaml"
)
D1_RULING = '2026-10-02T11:25:17Z | RULING | lane=claude-mods-plan | ticket=OMN-18531 | kind=decision | verbatim=encoded | "D1 (mod file shape / ratchet exception), plan beta/plans/2026-10-01-claude-code-context-and-delegation-adapter-plan.md, decision D1. Operator answer, 2026-10-02, in-session: %22mod file for what? nothing here should be non-canonical%22"'
D1_CORRECTION = "2026-10-02T11:30:10Z | CORRECTION | lane=claude-mods-plan | ticket=OMN-18531 | corrects=2026-10-02T11:25:17Z | RULING row 2026-10-02T11:25:17Z (decision D1) quotes caller framing inside its quoted cell; the operator's words are only the span after \"in-session:\", that is: mod file for what? nothing here should be non-canonical. The framing text before it is context, not the operator's words."


class _Reader:
    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        return tuple(
            ModelLedgerSourceRow(
                row_id=row_id_of(raw),
                raw_row=raw,
                source_ref=f"work_ledger_rows:{row_id_of(raw)}",
            )
            for raw in (D1_RULING, D1_CORRECTION)
        )


def _node() -> NodeContextSourceLedgerEffect:
    return NodeContextSourceLedgerEffect(reader=_Reader(), tenant_ids=("lab",))


def test_context_source_ledger_golden_chain_contract_topics() -> None:
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
    assert contract["output_model"].endswith("ModelLedgerSourceResult")


def test_context_source_ledger_golden_chain_real_runtime_typed_dispatch() -> None:
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
        factor=EnumContextFactor.OPERATOR_RULINGS,
        access=ModelContextAccess(tenant_id="lab"),
        revision="2026-10-02T12:00:00Z",
    )
    dispatch = asyncio.run(
        callback(
            {
                "payload": request.model_dump(mode="json"),
                "correlation_id": "00000000-0000-4000-8000-000000021127",
            }
        )
    )
    assert dispatch is not None
    terminal = ModelLedgerSourceResult.model_validate(dispatch.output_events[0])
    assert terminal.status is EnumContextSourceStatus.RESOLVED
    (entry,) = terminal.entries
    assert entry.text == D1_RULING
    assert entry.supersession_status is EnumContextSupersessionStatus.CORRECTED
    assert [c.text for c in entry.corrected_by] == [D1_CORRECTION]
