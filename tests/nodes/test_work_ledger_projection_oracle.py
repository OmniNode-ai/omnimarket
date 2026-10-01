"""Parity acceptance against the released work-ledger COMPUTE fold."""

from __future__ import annotations

import random

import pytest
from omnibase_core.models.nodes.work_ledger_state import ModelWorkLedgerFoldInput
from omnibase_core.nodes.node_work_ledger_state_compute.handler import (
    NodeWorkLedgerStateCompute,
)

from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    HandlerProjectionWorkLedger,
)
from omnimarket.nodes.node_projection_work_ledger.models import (
    ModelWorkLedgerProjectionRequest,
)
from tests.nodes.work_ledger_fixtures import (
    AS_OF,
    core_lines,
    records_for_all_ledger_row_types,
)


def _semantic_state_dump(state: object) -> dict[str, object]:
    return state.model_dump(mode="json", exclude={"line_count"})  # type: ignore[attr-defined]


def test_shuffled_and_redelivered_all_row_types_match_released_core_fold() -> None:
    records = records_for_all_ledger_row_types()
    core = NodeWorkLedgerStateCompute()
    projector = HandlerProjectionWorkLedger()

    # Include a redelivery of the claim and message events. Core state is set based;
    # line_count records transport deliveries and is intentionally excluded here.
    redelivered = (*records, records[1], records[5])
    expected = core.handle(
        ModelWorkLedgerFoldInput(lines=tuple(core_lines(redelivered)), as_of=AS_OF)
    )
    expected_semantics = _semantic_state_dump(expected)

    for seed in (0, 7, 41, 2026):
        shuffled = list(redelivered)
        random.Random(seed).shuffle(shuffled)
        history = []
        for record in shuffled:
            history.append(record)
            actual = projector.handle_records(
                ModelWorkLedgerProjectionRequest(records=tuple(history), as_of=AS_OF)
            ).state
            expected_prefix = core.handle(
                ModelWorkLedgerFoldInput(
                    lines=tuple(core_lines(tuple(history))), as_of=AS_OF
                )
            )
            assert actual == expected_prefix
        assert _semantic_state_dump(actual) == expected_semantics
        assert actual.event_count == expected.event_count
        assert actual.line_count == len(redelivered)


def test_duplicate_uuid_with_changed_canonical_content_fails_closed() -> None:
    record = records_for_all_ledger_row_types()[1]
    changed = record.model_copy(
        update={
            "event": record.event.model_copy(
                update={"summary": "changed immutable payload"}
            )
        }
    )
    projector = HandlerProjectionWorkLedger()
    with pytest.raises(ValueError, match="conflicting canonical content"):
        projector.rows_for_records((record, changed))
    with pytest.raises(ValueError, match="conflicting canonical content"):
        projector.fold_records((changed, record), as_of=AS_OF)
