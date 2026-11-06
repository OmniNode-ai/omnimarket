# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stale-hold rule's golden and error chains, and its overlay-held bound (OMN-22730).

Golden: the query command reaches the node through the real runtime typed dispatch and the
terminal event carries the in-force hold, the stale hold on its own line and an inbox that does
not count it. Error: a hold whose opening row is not a ledger row (the named injected break)
emits the typed error terminal event and never the golden answer. Bound: the age comes from the
contract's setting, so a 7 day setting turns a 13 day old hold stale and a 14 day setting does not.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerStateEntity,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    CORRELATION,
    NOW,
    FakeStateReader,
    node,
    request,
)
from tests.unit.nodes.node_work_ledger_query_effect.test_stale_holds import (
    CONTRACT,
    open_hold,
)

pytestmark = pytest.mark.unit

K = EnumWorkLedgerQueryKind


def _dispatch(handler: HandlerWorkLedgerQuery, kind: K, **fields: object) -> object:
    """The terminal event one query command produces through the runtime's typed dispatch."""
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    callback = _make_dispatch_callback(handler, event_model=route.event_model)
    command = request(kind, **fields)
    dispatch = asyncio.run(
        callback(
            {
                "payload": command.model_dump(mode="json"),
                "correlation_id": str(CORRELATION),
            }
        )
    )
    assert dispatch is not None
    return dispatch.output_events[0]


def test_golden_chain_the_terminal_event_names_the_stale_hold_apart() -> None:
    old = open_hold("hold-old", timedelta(days=39))
    young = open_hold("hold-young", timedelta(days=2))
    reader = FakeStateReader((old, young))
    terminal = ModelWorkLedgerQueryResult.model_validate(
        _dispatch(node(reader), K.HOLDS)
    )
    assert terminal.status == "ok"
    assert terminal.error is None
    assert terminal.correlation_id == CORRELATION
    assert [h.hold_id for h in terminal.holds] == ["hold-young"]
    assert any(line.startswith("HOLD stale=1") for line in terminal.lines)
    assert any("STALE hold-old" in line for line in terminal.lines)


def test_error_chain_a_hold_row_that_is_not_a_ledger_row_emits_the_error_event() -> (
    None
):
    good = open_hold("hold-young", timedelta(days=2))
    broken = open_hold("hold-broken", timedelta(days=39))
    broken = broken.model_copy(
        update={
            "opened_row": broken.opened_row.model_copy(
                update={"text": "this line is not a ledger row"}
            )
        }
    )
    terminal = ModelWorkLedgerQueryResult.model_validate(
        _dispatch(node(FakeStateReader((good, broken))), K.HOLDS)
    )
    assert terminal.status == "error"
    assert terminal.error is not None
    assert "hold-broken" in terminal.error
    assert "is not a ledger row" in terminal.error
    assert terminal.holds == ()
    assert not terminal.lines


def _contract_with_bound(directory: Path, days: int) -> Path:
    body = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    body["work_ledger_query"]["hold_stale_after_days"] = days
    path = directory / "contract.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _holds_for(
    entities: tuple[ModelWorkLedgerStateEntity, ...], contract: Path
) -> ModelWorkLedgerQueryResult:
    handler = HandlerWorkLedgerQuery(
        contract_path=contract, reader=FakeStateReader(entities), now=lambda: NOW
    )
    return handler.handle(request(K.HOLDS))


@pytest.mark.parametrize(
    ("bound_days", "stale"),
    [(7, True), (14, False)],
    ids=["seven-days", "fourteen-days"],
)
def test_the_bound_comes_from_the_contract_setting_not_a_literal(
    tmp_path: Path, bound_days: int, stale: bool
) -> None:
    thirteen = open_hold("hold-thirteen", timedelta(days=13))
    result = _holds_for((thirteen,), _contract_with_bound(tmp_path, bound_days))
    assert [h.hold_id for h in result.holds] == ([] if stale else ["hold-thirteen"])
    assert any(line.startswith("HOLD stale=1") for line in result.lines) is stale
    if stale:
        assert f"older than {bound_days}d" in result.lines[1]
