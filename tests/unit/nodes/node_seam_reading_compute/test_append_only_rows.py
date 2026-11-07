# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-seam readings are stored append-only (base-first piece 3, OMN-22909).

Golden: a reading appends one row per seam and the same reading read again is already written.
Error chain: a row whose key is already stored with different content is a rewrite, and the store
refuses it and leaves the old row as it was. The store has no update and no delete.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_seam_reading_compute.adapters.inmemory_seam_row_store import (
    InMemorySeamRowStore,
)
from omnimarket.nodes.node_seam_reading_compute.handlers.handler_seam_row_append import (
    HandlerSeamRowAppend,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_required_sender import (
    EnumSeamTrigger,
)
from omnimarket.nodes.node_seam_reading_compute.models.model_seam_row import (
    EnumRowAppendStatus,
    ModelSeamRow,
    ModelSeamRowAppendRequest,
)
from omnimarket.nodes.node_seam_reading_compute.protocols.protocol_seam_row_store import (
    ProtocolSeamRowStore,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 7, 14, 0, tzinfo=UTC)


def _row(
    seam: str = "review-dispatch",
    verdict: EnumReadingVerdict = EnumReadingVerdict.NOT_WORKING,
    reason: str = "no-bound-sender",
    probed_at: datetime = T0,
) -> ModelSeamRow:
    return ModelSeamRow.of(
        seam=seam,
        trigger=EnumSeamTrigger.AUTOMATIC,
        verdict=verdict,
        reason=reason,
        probed_at=probed_at,
    )


def test_append_only_rows_accumulate_and_keep_their_order() -> None:
    store = InMemorySeamRowStore()
    first = _row(probed_at=T0)
    second = _row(probed_at=T0.replace(hour=15))

    first_status = store.append(first)
    second_status = store.append(second)
    assert first_status is EnumRowAppendStatus.APPENDED
    assert second_status is EnumRowAppendStatus.APPENDED

    assert store.rows("review-dispatch") == (first, second)


def test_append_only_the_same_row_twice_is_already_appended_not_duplicated() -> None:
    store = InMemorySeamRowStore()
    row = _row()

    store.append(row)

    status = store.append(row)
    assert status is EnumRowAppendStatus.ALREADY_APPENDED
    assert store.rows("review-dispatch") == (row,)


def test_append_only_a_rewrite_of_an_old_row_is_refused_and_the_old_row_stays() -> None:
    store = InMemorySeamRowStore()
    old = _row(verdict=EnumReadingVerdict.NOT_WORKING)
    store.append(old)
    rewrite = ModelSeamRow(
        key=old.key,  # same seam and instant, changed content
        seam=old.seam,
        trigger=old.trigger,
        verdict=EnumReadingVerdict.WORKING,
        reason="sender-bound",
        probed_at=old.probed_at,
    )

    status = store.append(rewrite)
    assert status is EnumRowAppendStatus.REFUSED_REWRITE
    assert store.rows("review-dispatch") == (old,)


def test_append_only_the_store_exposes_no_way_to_change_or_remove_a_row() -> None:
    surface = {name for name in dir(ProtocolSeamRowStore) if not name.startswith("_")}
    assert surface == {"append", "rows"}
    store = InMemorySeamRowStore()
    assert not any(
        hasattr(store, n) for n in ("update", "delete", "remove", "clear", "pop")
    )


def test_append_only_rows_cannot_be_mutated_through_the_returned_tuple() -> None:
    store = InMemorySeamRowStore()
    store.append(_row())

    with pytest.raises(AttributeError):
        store.rows("review-dispatch").append(_row())  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        store.rows("review-dispatch")[0].reason = "changed"  # type: ignore[misc]


def test_append_only_golden_chain_events_end_in_the_appended_event() -> None:
    topics = load_topics()
    handler = HandlerSeamRowAppend(store=InMemorySeamRowStore())
    request = ModelSeamRowAppendRequest(
        correlation_id="rows-1", rows=(_row("a"), _row("b"))
    )

    out = answer_command(request.model_dump_json().encode(), handler, topics)

    events = [(t, json.loads(b)) for t, b in out]
    assert events[-1][0] == topics["seam_rows_appended"]
    assert events[-1][1]["appended"] == 2
    assert events[-1][1]["refused"] == 0


def test_append_only_error_chain_a_rewrite_ends_in_the_refused_event() -> None:
    topics = load_topics()
    store = InMemorySeamRowStore()
    handler = HandlerSeamRowAppend(store=store)
    old = _row()
    handler.handle(ModelSeamRowAppendRequest(correlation_id="rows-1", rows=(old,)))
    rewrite = ModelSeamRow(
        key=old.key,
        seam=old.seam,
        trigger=old.trigger,
        verdict=EnumReadingVerdict.WORKING,
        reason="sender-bound",
        probed_at=old.probed_at,
    )

    out = answer_command(
        ModelSeamRowAppendRequest(correlation_id="rows-2", rows=(rewrite,))
        .model_dump_json()
        .encode(),
        handler,
        topics,
    )

    events = [(t, json.loads(b)) for t, b in out]
    assert events[-1][0] == topics["seam_rows_refused"]
    assert events[-1][1]["failure"] == f"rewrite-refused:{old.key}"
    assert store.rows(old.seam) == (old,)


def test_append_only_error_chain_a_bad_payload_is_typed_never_dropped() -> None:
    topics = load_topics()
    handler = HandlerSeamRowAppend(store=InMemorySeamRowStore())

    out = answer_command(b"not json", handler, topics)

    assert out[0][0] == topics["seam_rows_refused"]
    assert json.loads(out[0][1])["failure"].startswith("bad-payload:")
