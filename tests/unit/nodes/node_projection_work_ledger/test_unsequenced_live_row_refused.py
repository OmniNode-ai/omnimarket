# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A live dual-write row with no ledger_seq is sequenced by the projection, or refused to the poison DLQ (OMN-23488, OMN-24634).

OMN-24634 amends the OMN-23488 behaviour on purpose: a row of a source in ``sequence_assigned_sources``
is given a provisional ledger_seq (the next after the tip; the judge's own number outranks it, see
test_provisional_sequence_next_judged.py) instead of being refused. A listed source with no declared way
to order it is still refused to the poison DLQ with its reason, and counted."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType,
)
from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    UnsequencedLiveRowError,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_projection import (
    ModelWorkLedgerProjectionConfig,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    parse_ledger_row,
)
from omnimarket.projection.error_classification import (
    ProjectionErrorClass,
    classify_projection_error,
)
from tests.unit.nodes.node_projection_work_ledger.test_work_ledger_writer import (
    _FakeDb,
    _writer,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_projection_work_ledger"
)
CLAIM = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"


def _unassigned(writer: Any) -> Any:
    """A writer whose contract lists onex-ledger as required but names no way to order it."""
    writer._config = writer._config.model_copy(update={"sequence_assigned_sources": ()})
    return writer


def _live_row(**extra: Any) -> dict[str, Any]:
    return {
        "raw_row": CLAIM,
        "_topic": EnumLedgerRowType.CLAIM.topic,
        "source": "onex-ledger",
        "ledger_seq": None,
        **extra,
    }


def test_a_live_unsequenced_row_is_sequenced_and_lands_with_a_ledger_seq() -> None:
    db = _FakeDb()
    writer = _writer(db)
    result = writer.handle(_live_row())
    assert result["rows_upserted"] == 2
    row_sql, row_args = db.statements[0]
    assert "work_ledger_rows" in row_sql
    assert (
        row_args[9] == 1
    )  # the ledger_seq column, assigned (an empty ledger -> first)
    assert (
        row_args[10] is True
    )  # provisional: the judge's own number for it outranks this one
    assert db.locks == [("work-ledger:rolling-work-ledger",)]
    assert (
        writer.unsequenced_live_rows_in,
        writer.unsequenced_live_rows_sequenced,
        writer.unsequenced_live_rows_dead_lettered,
    ) == (1, 1, 0)


def test_a_live_unsequenced_row_takes_the_next_sequence_after_the_ledger_tip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.nodes.node_projection_work_ledger import (
        test_work_ledger_writer as w,
    )

    async def fetchval(self: Any, sql: str, *args: Any) -> Any:
        if "COALESCE(MAX(ledger_seq)" in sql:
            return 42
        return None

    monkeypatch.setattr(w._FakeConnection, "fetchval", fetchval)
    db = _FakeDb()
    _writer(db).handle(_live_row())
    assert db.statements[0][1][9] == 42


def test_a_redelivered_live_unsequenced_row_keeps_its_stored_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.nodes.node_projection_work_ledger import (
        test_work_ledger_writer as w,
    )

    async def fetchval(self: Any, sql: str, *args: Any) -> Any:
        if "SELECT ledger_seq FROM" in sql:
            return 7
        return None

    monkeypatch.setattr(w._FakeConnection, "fetchval", fetchval)
    db = _FakeDb()
    _writer(db).handle(_live_row())
    assert db.statements[0][1][9] == 7


def test_unsequenced_dead_letter_a_source_with_no_declared_order_is_refused_and_counted() -> (
    None
):
    db = _FakeDb()
    writer = _unassigned(_writer(db))
    with pytest.raises(UnsequencedLiveRowError) as exc_info:
        writer.handle(_live_row())
    assert db.statements == []
    assert db.locks == []
    assert classify_projection_error(exc_info.value) is ProjectionErrorClass.POISON
    assert "sequence_assigned_sources" in str(exc_info.value)
    assert (
        writer.unsequenced_live_rows_in,
        writer.unsequenced_live_rows_sequenced,
        writer.unsequenced_live_rows_dead_lettered,
    ) == (1, 0, 1)


def _stateful_holds(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """A fake ledger that remembers the sequence each row was given, as the real table does."""
    from tests.unit.nodes.node_projection_work_ledger import (
        test_work_ledger_writer as w,
    )

    held: dict[str, int] = {}

    async def fetchval(self: Any, sql: str, *args: Any) -> Any:
        if "COALESCE(MAX(ledger_seq)" in sql:
            return len(held) + 1
        if "SELECT ledger_seq FROM" in sql:
            return held.get(args[0])
        return None

    monkeypatch.setattr(w._FakeConnection, "fetchval", fetchval)
    return held


def _remember(held: dict[str, int], writer: Any, raw: str) -> None:
    """Record, as the table would, the number the last delivery of ``raw`` gave it."""
    from omnimarket.handlers.work_ledger_fold import fold_row
    from omnimarket.models.model_work_ledger_fold_request import (
        ModelWorkLedgerFoldRequest,
    )

    row_id = fold_row(
        ModelWorkLedgerFoldRequest(raw_row=raw, source="onex-ledger")
    ).row.row_id
    held.setdefault(row_id, len(held) + 1)


def test_redelivery_counts_a_newly_assigned_number_once_and_the_redeliveries_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of omnimarket#6306, finding 4: three deliveries of one row counted three sequenced."""
    held = _stateful_holds(monkeypatch)
    writer = _writer(_FakeDb())
    for _ in range(3):
        result = writer.handle(_live_row())
        _remember(held, writer, CLAIM)
    assert (
        writer.unsequenced_live_rows_in,
        writer.unsequenced_live_rows_sequenced,
        writer.unsequenced_live_rows_redelivered,
        writer.unsequenced_live_rows_dead_lettered,
    ) == (3, 1, 2, 0)
    # The counts ride beside rows_upserted in the result the runtime reads.
    assert result["rows_upserted"] == 2
    assert result["unsequenced_live_rows_in"] == 3
    assert result["unsequenced_live_rows_sequenced"] == 1
    assert result["unsequenced_live_rows_redelivered"] == 2
    assert result["unsequenced_live_rows_dead_lettered"] == 0
    assert result["provisional_rows_moved"] == 0


def test_redelivery_mixed_batch_settles_every_delivery_as_one_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distinct rows, one redelivered, two refused: 5 in, and in is the sum of three outcomes read apart."""
    held = _stateful_holds(monkeypatch)
    other = CLAIM.replace("OMN-1", "OMN-2")
    writer = _writer(_FakeDb())
    for raw in (CLAIM, other, CLAIM):
        writer.handle(_live_row(raw_row=raw))
        _remember(held, writer, raw)
    _unassigned(writer)
    for _ in range(2):
        with pytest.raises(UnsequencedLiveRowError):
            writer.handle(_live_row())
    assert writer.unsequenced_live_rows_in == 5
    assert writer.unsequenced_live_rows_sequenced == 2
    assert writer.unsequenced_live_rows_redelivered == 1
    assert writer.unsequenced_live_rows_dead_lettered == 2
    assert (
        writer.unsequenced_live_rows_sequenced
        + writer.unsequenced_live_rows_redelivered
        + writer.unsequenced_live_rows_dead_lettered
        == writer.unsequenced_live_rows_in
    )


def test_a_failed_write_is_counted_in_and_settles_as_no_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database failure leaves no outcome count, so ``in`` above the outcomes is an unfinished delivery."""
    db = _FakeDb()

    async def boom(sql: str, *args: Any) -> None:
        raise RuntimeError("database refused the write")

    monkeypatch.setattr(db, "execute", boom)
    writer = _writer(db)
    with pytest.raises(RuntimeError, match="database refused"):
        writer.handle(_live_row())
    assert writer.unsequenced_live_rows_in == 1
    assert writer.unsequenced_live_rows_sequenced == 0
    assert writer.unsequenced_live_rows_redelivered == 0
    assert writer.unsequenced_live_rows_dead_lettered == 0


def test_assigned_sources_outside_required_sources_are_rejected() -> None:
    """Review of omnimarket#6306, finding 5: a source the projection would sequence must be one that needs it."""
    with pytest.raises(ValidationError, match="not in sequence_required_sources"):
        ModelWorkLedgerProjectionConfig.model_validate(
            {
                "ledger_id": "rolling-work-ledger",
                "sequence_required_sources": ["onex-ledger"],
                "sequence_assigned_sources": ["onex-ledger", "ledger-emit"],
                "latest_row_types": ["CLAIM"],
            }
        )
    accepted = ModelWorkLedgerProjectionConfig.model_validate(
        {
            "ledger_id": "rolling-work-ledger",
            "sequence_required_sources": ["onex-ledger"],
            "sequence_assigned_sources": ["onex-ledger"],
            "latest_row_types": ["CLAIM"],
        }
    )
    assert accepted.sequence_assigned_sources == ("onex-ledger",)


def test_assigned_sources_in_the_shipped_contract_are_a_subset_of_the_required_sources() -> (
    None
):
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())["work_ledger"]
    assert set(contract["sequence_assigned_sources"]) <= set(
        contract["sequence_required_sources"]
    )
    ModelWorkLedgerProjectionConfig.model_validate(contract)


def test_a_live_source_row_with_a_ledger_seq_is_projected() -> None:
    db = _FakeDb()
    result = _writer(db).handle(
        {
            "raw_row": CLAIM,
            "_topic": EnumLedgerRowType.CLAIM.topic,
            "source": "onex-ledger",
            "ledger_seq": 17,
        }
    )
    assert result["rows_upserted"] == 2


def test_the_backfill_source_is_not_listed_and_projects_unsequenced() -> None:
    db = _FakeDb()
    result = _writer(db).handle(
        {
            "raw_row": CLAIM,
            "_topic": EnumLedgerRowType.CLAIM.topic,
            "source": "ledger-emit",
            "ledger_seq": None,
        }
    )
    assert result["rows_upserted"] == 2


def test_an_unlisted_source_is_projected_even_without_a_seq() -> None:
    db = _FakeDb()
    writer = _writer(db)
    writer._config = writer._config.model_copy(update={"sequence_required_sources": ()})
    result = writer.handle(
        {
            "raw_row": CLAIM,
            "_topic": EnumLedgerRowType.CLAIM.topic,
            "source": "onex-ledger",
            "ledger_seq": None,
        }
    )
    assert result["rows_upserted"] == 2


def test_the_contract_declares_the_required_sources_and_the_config_requires_the_field() -> (
    None
):
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert contract["work_ledger"]["sequence_required_sources"] == ["onex-ledger"]
    assert contract["work_ledger"]["sequence_assigned_sources"] == ["onex-ledger"]
    with pytest.raises(ValidationError, match="sequence_required_sources"):
        ModelWorkLedgerProjectionConfig.model_validate(
            {"ledger_id": "rolling-work-ledger"}
        )


def test_unsequenced_dead_letter_row_is_published_to_the_contract_dlq_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error half: the poison route puts the refusal and its reason on the declared DLQ."""
    published: list[tuple[str, bytes]] = []

    async def publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    async def get_publish_fn() -> object:
        return publish

    db = _FakeDb()
    writer = _unassigned(_writer(db))
    monkeypatch.setattr(writer, "get_publish_fn", get_publish_fn)
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert writer.poison_dlq_topics == contract["event_bus"]["dlq_topics"]
    data = {
        "raw_row": CLAIM,
        "_topic": EnumLedgerRowType.CLAIM.topic,
        "source": "onex-ledger",
        "ledger_seq": None,
    }
    with pytest.raises(UnsequencedLiveRowError) as exc_info:
        writer.handle(data)
    routed = asyncio.run(
        writer._route_poison_to_dlq(
            EnumLedgerRowType.CLAIM.topic,
            data,
            exc_info.value,
            MessageMeta(partition=0, offset=7, fallback_id="t"),
        )
    )
    assert routed is True
    assert db.statements == []
    assert len(published) == 1
    topic, value = published[0]
    assert topic.endswith(contract["event_bus"]["dlq_topics"][0])
    assert b"unsequenced-live-row" in value


def _live_envelope_without_seq() -> dict[str, object]:
    """The envelope a live dual-write row has when it holds no sequence: the typed event, source onex-ledger.

    Derived from the event builder the emit node publishes with (the dual write builds the same
    ledger_id, raw_row, row_id, row_timestamp, row_type and source; read from the database on
    2026-11-10), so a field renamed there turns the seam test red.
    """
    return parse_ledger_row(CLAIM, source="onex-ledger").model_dump(mode="json")


def test_seam_the_live_dual_write_envelope_without_a_seq_is_sequenced_and_with_one_keeps_it() -> (
    None
):
    topic = EnumLedgerRowType.CLAIM.topic
    sequenced_db = _FakeDb()
    sequenced = _writer(sequenced_db).handle(
        {**_live_envelope_without_seq(), "_topic": topic}
    )
    assert sequenced["rows_upserted"] == 2
    assert sequenced_db.statements[0][1][9] == 1
    taken_db = _FakeDb()
    taken = _writer(taken_db).handle(
        {**_live_envelope_without_seq(), "ledger_seq": 9, "_topic": topic}
    )
    assert taken["rows_upserted"] == 2
    assert taken_db.statements[0][1][9] == 9
    assert taken_db.statements[0][1][10] is False


def test_seam_the_emit_node_default_source_is_not_a_refused_source() -> None:
    """The emit node (backfill, the skill) publishes with no sequence; its default source must stay unlisted."""
    from omnimarket.nodes.node_projection_work_ledger.parity_receipt import (
        BACKFILL_SOURCES,
    )
    from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
        ModelWorkLedgerEmitRequest,
    )

    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    required = set(contract["work_ledger"]["sequence_required_sources"])
    default_source = ModelWorkLedgerEmitRequest(row=CLAIM).source
    assert default_source not in required
    assert not (BACKFILL_SOURCES & required)
    db = _FakeDb()
    result = _writer(db).handle(
        {
            **_live_envelope_without_seq(),
            "source": default_source,
            "_topic": EnumLedgerRowType.CLAIM.topic,
        }
    )
    assert result["rows_upserted"] == 2
