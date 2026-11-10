# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A live dual-write row with no ledger_seq is refused to the poison DLQ (OMN-23488)."""

from __future__ import annotations

import asyncio
from pathlib import Path

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


def test_a_live_source_row_with_no_ledger_seq_is_refused_before_any_write() -> None:
    db = _FakeDb()
    writer = _writer(db)
    with pytest.raises(UnsequencedLiveRowError) as exc_info:
        writer.handle(
            {
                "raw_row": CLAIM,
                "_topic": EnumLedgerRowType.CLAIM.topic,
                "source": "onex-ledger",
                "ledger_seq": None,
            }
        )
    assert db.statements == []
    assert db.locks == []
    assert classify_projection_error(exc_info.value) is ProjectionErrorClass.POISON


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
    assert result == {"rows_upserted": 2}


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
    assert result == {"rows_upserted": 2}


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
    assert result == {"rows_upserted": 2}


def test_the_contract_declares_the_required_sources_and_the_config_requires_the_field() -> (
    None
):
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert contract["work_ledger"]["sequence_required_sources"] == ["onex-ledger"]
    with pytest.raises(ValidationError, match="sequence_required_sources"):
        ModelWorkLedgerProjectionConfig.model_validate(
            {"ledger_id": "rolling-work-ledger"}
        )


def test_the_refused_row_is_published_to_the_contract_dlq_topic() -> None:
    """The error half: the poison route puts the refusal and its reason on the declared DLQ."""
    published: list[tuple[str, bytes]] = []

    async def publish(topic: str, value: bytes) -> None:
        published.append((topic, value))

    async def get_publish_fn() -> object:
        return publish

    db = _FakeDb()
    writer = _writer(db)
    writer.get_publish_fn = get_publish_fn  # type: ignore[method-assign]
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


def test_seam_the_live_dual_write_envelope_without_a_seq_is_refused_and_with_one_is_taken() -> (
    None
):
    topic = EnumLedgerRowType.CLAIM.topic
    refused_db = _FakeDb()
    with pytest.raises(UnsequencedLiveRowError):
        _writer(refused_db).handle({**_live_envelope_without_seq(), "_topic": topic})
    assert refused_db.statements == []
    taken_db = _FakeDb()
    taken = _writer(taken_db).handle(
        {**_live_envelope_without_seq(), "ledger_seq": 9, "_topic": topic}
    )
    assert taken == {"rows_upserted": 2}


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
    assert result == {"rows_upserted": 2}
