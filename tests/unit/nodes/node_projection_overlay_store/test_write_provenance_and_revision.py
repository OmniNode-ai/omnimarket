# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Design rules 1 and 2: provenance per path, one revision per overlay checked at the store, a reply carrying the write id."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_store import (
    EnumOverlayValueSource,
    EnumOverlayWriteOutcome,
    ModelOverlayStoreWrite,
    ModelOverlayWriteReply,
)
from tests.unit.nodes.node_projection_overlay_store.support import (
    AUDIT_TOPIC,
    NOW,
    OVERLAY_ID,
    PATH_LANE,
    PATH_OWNER,
    REPLY_TOPIC,
    SNAPSHOT_TOPIC,
    Loopback,
    default_document,
    set_write,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_set_is_stored_with_provenance_per_path_and_a_revision() -> None:
    loop = Loopback()
    await loop.seed()
    assert loop.snapshot()["revision"] == 1
    assert loop.snapshot()["operator_paths"] == []
    await loop.deliver(set_write(based_on_revision=1))

    snapshot = loop.snapshot()
    assert snapshot["revision"] == 2
    assert snapshot["operator_paths"] == [PATH_OWNER]
    assert snapshot["document"] == {**default_document(), "owner": "owner-b"}
    provenance = await loop.store.read_provenance(OVERLAY_ID)
    # Only the operator's path carries an entry; every other path is a default value of the record's source_ref.
    assert set(provenance) == {PATH_OWNER}
    entry = provenance[PATH_OWNER]
    assert entry.source is EnumOverlayValueSource.OPERATOR
    assert (entry.actor, entry.revision, entry.write_id) == ("ccp3-test", 2, "w-set")


@pytest.mark.asyncio
async def test_a_write_built_on_a_stale_revision_is_refused_with_a_conflict_reply_carrying_its_id() -> (
    None
):
    loop = Loopback()
    await loop.seed()
    await loop.deliver(set_write(write_id="w-first", based_on_revision=1))
    before = dict(loop.snapshot())

    await loop.deliver(
        set_write(
            write_id="w-late",
            based_on_revision=1,
            key="alarm_lane",
            path=PATH_LANE,
            value="lane-z",
            loaded_at=NOW.replace(minute=5),
        )
    )

    replies = [ModelOverlayWriteReply.model_validate(r) for r in loop.on(REPLY_TOPIC)]
    assert [(r.write_id, r.outcome, r.revision) for r in replies] == [
        ("w-first", EnumOverlayWriteOutcome.COMMITTED, 2),
        ("w-late", EnumOverlayWriteOutcome.CONFLICT, 2),
    ]
    # The conflicted write changed nothing: the record, its provenance and the audit rows are as they were.
    assert await loop.store.read_current(OVERLAY_ID) == before
    assert set(await loop.store.read_provenance(OVERLAY_ID)) == {PATH_OWNER}
    assert loop.store.audit_write_ids == ("w-first",)
    assert [a["actor"] for a in loop.on(AUDIT_TOPIC)] == ["ccp3-test"]


@pytest.mark.asyncio
async def test_the_snapshot_is_published_with_every_write_and_a_load_still_stores() -> (
    None
):
    loop = Loopback()
    await loop.seed()
    await loop.deliver(set_write(based_on_revision=1))
    assert len(loop.on(SNAPSHOT_TOPIC)) == 2
    # A loader's load names no write id: it is applied as before and answered with no reply.
    assert len(loop.on(REPLY_TOPIC)) == 1


def test_operator_paths_without_an_audit_row_are_refused() -> None:
    write = set_write()
    del write["audit"]
    with pytest.raises(ValidationError, match="without an audit row"):
        ModelOverlayStoreWrite.model_validate(write)


def test_an_audit_row_without_the_revision_it_was_built_on_is_refused() -> None:
    write = set_write()
    del write["based_on_revision"]
    with pytest.raises(ValidationError, match="revision it was built on"):
        ModelOverlayStoreWrite.model_validate(write)
