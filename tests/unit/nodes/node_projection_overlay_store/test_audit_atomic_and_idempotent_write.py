# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record, its provenance and the audit row are one transaction; a write id is applied once."""

from __future__ import annotations

from datetime import datetime

import pytest

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import (
    EnumOverlayWriteOutcome,
    ModelOverlaySettingAudit,
    ModelOverlayWriteReply,
)
from tests.unit.nodes.node_projection_overlay_store.support import (
    AUDIT_TOPIC,
    OVERLAY_ID,
    PATH_OWNER,
    REPLY_TOPIC,
    SNAPSHOT_TOPIC,
    Loopback,
    default_document,
    set_write,
)

pytestmark = pytest.mark.unit


class AuditWriteFails(InMemoryOverlayStore):
    """A store whose audit write fails, as the audit table's insert does on a violated constraint."""

    def _write_audit(
        self,
        audits: dict[str, ModelOverlaySettingAudit],
        write_id: str,
        audit: ModelOverlaySettingAudit,
    ) -> None:
        raise ConnectionError("the audit row could not be written")


@pytest.mark.asyncio
async def test_audit_atomic_a_failed_audit_write_leaves_the_record_unchanged() -> None:
    store = AuditWriteFails()
    loop = Loopback(store)
    await loop.seed()
    before = await store.read_current(OVERLAY_ID)
    assert before is not None
    snapshots, replies = len(loop.on(SNAPSHOT_TOPIC)), len(loop.on(REPLY_TOPIC))

    with pytest.raises(ConnectionError):
        await loop.deliver(set_write(based_on_revision=1))

    # The record, its revision and its provenance are as they were; no audit row, no reply and no snapshot went out.
    assert await store.read_current(OVERLAY_ID) == before
    assert before["document"] == default_document()
    assert before["operator_paths"] == []
    assert await store.read_provenance(OVERLAY_ID) == {}
    assert store.audit_write_ids == ()
    assert (len(loop.on(SNAPSHOT_TOPIC)), len(loop.on(REPLY_TOPIC))) == (
        snapshots,
        replies,
    )
    assert loop.on(AUDIT_TOPIC) == []


@pytest.mark.asyncio
async def test_audit_atomic_the_same_write_applies_once_the_audit_write_works() -> None:
    """Positive control: the failure above is the audit write, not the write: on a working store it is applied."""
    loop = Loopback()
    await loop.seed()
    await loop.deliver(set_write(based_on_revision=1))
    assert loop.store.audit_write_ids == ("w-set",)
    assert (await loop.store.read_current(OVERLAY_ID) or {})["operator_paths"] == [
        PATH_OWNER
    ]


@pytest.mark.asyncio
async def test_idempotent_write_delivered_twice_is_applied_once() -> None:
    loop = Loopback()
    await loop.seed()
    write = set_write(based_on_revision=1)
    await loop.deliver(write)
    after_first = await loop.store.read_current(OVERLAY_ID)
    await loop.deliver(write)

    assert await loop.store.read_current(OVERLAY_ID) == after_first
    assert (after_first or {})["revision"] == 2
    assert loop.store.audit_write_ids == ("w-set",)
    assert len(loop.on(AUDIT_TOPIC)) == 1
    replies = [ModelOverlayWriteReply.model_validate(r) for r in loop.on(REPLY_TOPIC)]
    assert [(r.outcome, r.revision, r.replayed) for r in replies] == [
        (EnumOverlayWriteOutcome.COMMITTED, 2, False),
        (EnumOverlayWriteOutcome.COMMITTED, 2, True),
    ]


@pytest.mark.asyncio
async def test_idempotent_write_the_audit_row_a_failed_publish_missed_goes_out_on_redelivery() -> (
    None
):
    loop = Loopback(fail_publish_on=AUDIT_TOPIC)
    await loop.seed()
    write = set_write(based_on_revision=1)
    with pytest.raises(ConnectionError):
        await loop.deliver(write)
    # The transaction committed; the audit topic did not get the row, and no reply went out.
    assert loop.store.audit_write_ids == ("w-set",)
    assert loop.on(AUDIT_TOPIC) == []

    await loop.deliver(write)
    assert len(loop.on(AUDIT_TOPIC)) == 1
    assert loop.store.audit_write_ids == ("w-set",)
    await loop.deliver(write)
    assert len(loop.on(AUDIT_TOPIC)) == 1


@pytest.mark.asyncio
async def test_idempotent_write_a_lost_reply_is_answered_again_by_the_same_write_id() -> (
    None
):
    """The set command publishes the same write again when no reply came; the store applies it once."""
    loop = Loopback(drop_replies=1)
    await loop.seed()
    handler = loop.handler(reply_timeout_s=0.2, write_ids=["w-one"])
    from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
        ModelOverlaySettingSetRequest,
    )

    result = await handler.handle(
        ModelOverlaySettingSetRequest(
            overlay_id=OVERLAY_ID, key="owner", value="owner-b", actor="ccp3-test"
        )
    )
    assert result.write_id == "w-one"
    assert [d["write_id"] for d in loop.delivered] == ["w-one", "w-one"]
    assert loop.store.audit_write_ids == ("w-one",)
    assert (await loop.store.read_current(OVERLAY_ID) or {})["revision"] == 2
    assert isinstance(
        datetime.fromisoformat(loop.on(AUDIT_TOPIC)[0]["set_at"]), datetime
    )
