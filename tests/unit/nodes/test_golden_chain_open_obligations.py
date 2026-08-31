# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for the open-obligations projection (OMN-17019 / C9).

Chain under test, end to end with no Kafka and no Postgres:

    obligation lifecycle payloads -> EventBusInmemory.publish/consume
        -> HandlerProjectionOpenObligations.project
        -> InmemoryDatabaseAdapter row in omninode_internal.open_obligations
        -> render_open_obligations markdown
        -> parse_open_obligations round trip

The chain is driven through a WHOLE LIFECYCLE, not a single event, because the
thing worth proving about this node is the FOLD: five events, one row, and a
"what is currently owed" answer that survives the events arriving twice and out
of order. A single-event golden chain would prove nothing this node is for.

The in-memory adapter cannot evaluate the two GENERATED columns (``state`` and
``owed_by``) -- those are Postgres expressions, and the real-column proof lives
in ``tests/test_omn17019_real_postgres_open_obligations_write_path.py``. Here
the chain asserts the FACTS the generation reads from (``closed_state``,
``transferred_owed_by``, ``original_owed_by``), which is the half this layer can
honestly prove.

Field-level assertions throughout -- never merely "some rows were written".
"""

from __future__ import annotations

import json

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_projection_open_obligations.handlers.handler_projection_open_obligations import (
    _DEFAULT_CONTRACT_PATH,
    _TOPIC_KIND,
    TABLE,
    HandlerProjectionOpenObligations,
)
from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    EnumObligationEventKind,
    EnumObligationState,
    ModelObligationEventInbound,
)
from omnimarket.nodes.node_projection_open_obligations.models.model_open_obligation_view import (
    ModelOpenObligationView,
)
from omnimarket.nodes.node_projection_open_obligations.obligations_view import (
    parse_open_obligations,
    render_open_obligations,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

# The terminal deterministic-truth assertion the RUNTIME publishes when a
# durable row lands (contract event_bus.publish_topics). Named here so the
# contract-state-coverage gate can see this node's declared output state is
# asserted by a test rather than merely declared.
APPLIED_TOPIC = "onex.evt.omnimarket.projection-open-obligations-applied.v1"

_TOPIC_BY_KIND: dict[EnumObligationEventKind, str] = {
    kind: topic for topic, kind in _TOPIC_KIND.items()
}

_OBLIGATION = "omn17019-golden-chain"

# One obligation's whole life, in partition-offset order. Payloads are the flat
# JSON the emit daemon fans out for each work.obligation.* event type, carrying
# exactly the fields registries/topics.yaml declares required for that type.
_WIRE: tuple[tuple[EnumObligationEventKind, str], ...] = (
    (
        EnumObligationEventKind.CREATED,
        json.dumps(
            {
                "obligation_id": _OBLIGATION,
                "emitted_at": "2026-08-30T09:00:00+00:00",
                "actor_id": "session-morning",
                "actor_kind": "session",
                "summary": "send the beta readiness brief to the operator",
                "asked_by": "operator",
                "owed_by": "session-morning",
                "acceptance_condition": "brief delivered and acknowledged",
                "ticket_id": "OMN-17019",
            }
        ),
    ),
    (
        EnumObligationEventKind.TRANSFERRED,
        json.dumps(
            {
                "obligation_id": _OBLIGATION,
                "emitted_at": "2026-08-30T11:30:00+00:00",
                "actor_id": "session-morning",
                "actor_kind": "session",
                "summary": "handing the brief to the afternoon lane",
                "owed_by": "session-afternoon",
            }
        ),
    ),
    (
        EnumObligationEventKind.SATISFIED,
        json.dumps(
            {
                "obligation_id": _OBLIGATION,
                "emitted_at": "2026-08-30T16:45:00+00:00",
                "actor_id": "session-afternoon",
                "actor_kind": "session",
                "summary": "brief delivered",
                "evidence_uri": "https://example.invalid/beta-readiness-brief.md",
                "delivery_state": "sent",
            }
        ),
    ),
)


def _project_all(db: InmemoryDatabaseAdapter) -> None:
    handler = HandlerProjectionOpenObligations()
    for kind, raw in _WIRE:
        handler.project(
            ModelObligationEventInbound(**json.loads(raw)), db, _TOPIC_BY_KIND[kind]
        )


def _sole_row(db: InmemoryDatabaseAdapter) -> dict[str, object]:
    rows = db.query(TABLE)
    assert len(rows) == 1, f"the fold must produce ONE row, got {len(rows)}"
    return rows[0]


@pytest.mark.unit
def test_contract_declares_the_applied_topic_the_runtime_publishes() -> None:
    """The node's only output state, asserted rather than assumed.

    The handler must NOT publish it -- handler_wiring emits it only when the
    returned result carries rows_upserted >= 1, so the event asserts that a
    durable row landed rather than that handle() did not raise.
    """
    contract = yaml.safe_load(_DEFAULT_CONTRACT_PATH.read_text())
    assert contract["event_bus"]["publish_topics"] == [APPLIED_TOPIC]
    assert contract["externally_consumed_topics"] == [APPLIED_TOPIC]

    source = (
        _DEFAULT_CONTRACT_PATH.parent
        / "handlers"
        / "handler_projection_open_obligations.py"
    ).read_text()
    assert APPLIED_TOPIC not in source, "the runtime publishes it, not the handler"


@pytest.mark.unit
def test_a_whole_lifecycle_folds_to_one_row_with_field_exact_values() -> None:
    db = InmemoryDatabaseAdapter()
    _project_all(db)
    row = _sole_row(db)

    assert row["obligation_id"] == _OBLIGATION
    assert row["asked_by"] == "operator"
    assert row["acceptance_condition"] == "brief delivered and acknowledged"
    assert row["opened_summary"] == "send the beta readiness brief to the operator"
    assert row["ticket_id"] == "OMN-17019"
    # The two halves `owed_by` is generated from: the transfer wins, and the
    # original is still on the record rather than overwritten.
    assert row["original_owed_by"] == "session-morning"
    assert row["transferred_owed_by"] == "session-afternoon"
    # The fact `state` is generated from.
    assert row["closed_state"] == EnumObligationState.SATISFIED.value
    assert row["evidence_uri"] == "https://example.invalid/beta-readiness-brief.md"
    assert row["delivery_state"] == "sent"
    assert row["last_event_kind"] == EnumObligationEventKind.SATISFIED.value


@pytest.mark.unit
async def test_chain_through_the_event_bus_matches_direct_projection() -> None:
    """Routing the same payloads through the bus changes nothing.

    Guards the seam a sibling projection got wrong (OMN-16993): a handler that
    works when called directly but not on what the bus actually hands it.
    """
    bus = EventBusInmemory()
    await bus.start()
    try:
        for kind, raw in _WIRE:
            await bus.publish(_TOPIC_BY_KIND[kind], None, raw.encode("utf-8"))
        # Read the messages back OFF the bus rather than reusing the inputs --
        # otherwise this asserts nothing about what the bus actually carried.
        delivered = [
            (message.topic, message.value.decode("utf-8"))
            for message in await bus.get_event_history(limit=100)
        ]
    finally:
        await bus.shutdown()

    assert len(delivered) == len(_WIRE), "bus dropped a message"
    assert {topic for topic, _ in delivered} == {
        _TOPIC_BY_KIND[kind] for kind, _ in _WIRE
    }

    bus_db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionOpenObligations()
    for topic, raw in delivered:
        handler.project(ModelObligationEventInbound(**json.loads(raw)), bus_db, topic)

    direct_db = InmemoryDatabaseAdapter()
    _project_all(direct_db)

    assert bus_db.query(TABLE) == direct_db.query(TABLE)


@pytest.mark.unit
def test_replaying_the_whole_chain_out_of_order_does_not_reopen_the_close() -> None:
    """The chain's real acceptance: a rewound consumer must not lose the close.

    A consumer restarting from an earlier partition offset replays ``created``
    and ``transferred`` after ``satisfied`` has already been applied. The fold
    must still report the obligation closed, with its delivery evidence intact.
    """
    db = InmemoryDatabaseAdapter()
    _project_all(db)
    handler = HandlerProjectionOpenObligations()
    for kind, raw in _WIRE[:2]:
        handler.project(
            ModelObligationEventInbound(**json.loads(raw)), db, _TOPIC_BY_KIND[kind]
        )

    row = _sole_row(db)
    assert row["closed_state"] == EnumObligationState.SATISFIED.value
    assert row["evidence_uri"] == "https://example.invalid/beta-readiness-brief.md"
    assert row["transferred_owed_by"] == "session-afternoon"


@pytest.mark.unit
def test_chain_renders_an_open_obligations_view_that_round_trips() -> None:
    """The rendered view carries the real values and parses back to them.

    Two obligations, one still open and one closed by the chain above, so the
    render is proven to SELECT rather than to dump.
    """
    db = InmemoryDatabaseAdapter()
    _project_all(db)
    closed = _sole_row(db)

    # The read model as the projection_api would return it -- the generated
    # columns are computed here the way Postgres computes them, from the same
    # facts the chain wrote.
    rows = [
        ModelOpenObligationView(
            obligation_id=str(closed["obligation_id"]),
            state=EnumObligationState(str(closed["closed_state"])),
            last_event_at=closed["last_event_at"],  # type: ignore[arg-type]
            owed_by=str(closed["transferred_owed_by"]),
            asked_by=str(closed["asked_by"]),
            acceptance_condition=str(closed["acceptance_condition"]),
            evidence_uri=str(closed["evidence_uri"]),
            delivery_state=str(closed["delivery_state"]),
        ),
        ModelOpenObligationView(
            obligation_id="omn17019-still-open",
            state=EnumObligationState.OPEN,
            last_event_at=closed["last_event_at"],  # type: ignore[arg-type]
            owed_by="session-evening",
            asked_by="operator",
            acceptance_condition="ledger row appended",
        ),
    ]

    rendered = render_open_obligations(rows)
    assert "omn17019-still-open" in rendered
    assert _OBLIGATION not in rendered, "a satisfied obligation is not owed"

    reparsed = parse_open_obligations(rendered)
    assert [row.obligation_id for row in reparsed] == ["omn17019-still-open"]
    assert reparsed[0].owed_by == "session-evening"
    assert render_open_obligations(reparsed) == rendered
