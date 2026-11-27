# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two sets of different keys of one overlay, racing, both survive: the revision check turns the loser's write into a
conflict, and the set command builds it again on what the store then holds.

The overlay is a test overlay that declares two settable keys (``owner`` and ``alarm_lane`` of the delegation check
overlay) over an in-memory store; no real overlay or store is written.
"""

from __future__ import annotations

import asyncio

import pytest

from omnimarket.nodes.node_overlay_setting_set_effect.handlers.handler_overlay_setting_set import (
    SettingWriteConflictError,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from tests.unit.nodes.node_projection_overlay_store.support import (
    AUDIT_TOPIC,
    OVERLAY_ID,
    PATH_LANE,
    PATH_OWNER,
    Loopback,
)

pytestmark = pytest.mark.unit


def _request(key: str, value: str, actor: str) -> ModelOverlaySettingSetRequest:
    return ModelOverlaySettingSetRequest(
        overlay_id=OVERLAY_ID, key=key, value=value, actor=actor
    )


@pytest.mark.asyncio
async def test_concurrent_sets_of_different_keys_both_survive() -> None:
    # Both commands read the same snapshot before either writes, so the second write names a stale revision.
    loop = Loopback(gate_first_reads=2)
    await loop.seed()
    first = loop.handler(write_ids=["w-owner", "w-owner-again"])
    second = loop.handler(write_ids=["w-lane", "w-lane-again"])

    owner, lane = await asyncio.gather(
        first.handle(_request("owner", "owner-b", "actor-a")),
        second.handle(_request("alarm_lane", "lane-b", "actor-b")),
    )

    final = await loop.store.read_current(OVERLAY_ID)
    assert final is not None
    assert final["document"] == {"owner": "owner-b", "alarm_lane": "lane-b"}
    assert final["operator_paths"] == sorted([PATH_OWNER, PATH_LANE])
    assert final["revision"] == 3
    # The race happened: one write conflicted and was built again, and the revisions are one apart.
    assert sorted([owner.attempts, lane.attempts]) == [1, 2]
    assert sorted([owner.revision, lane.revision]) == [2, 3]
    assert loop.refreshes == 1
    # Each set left exactly one audit row, and the table holds each path's provenance.
    assert sorted(a["actor"] for a in loop.on(AUDIT_TOPIC)) == ["actor-a", "actor-b"]
    # The conflicted write left no row; the write built again on the new revision did, under a new id.
    stored = set(loop.store.audit_write_ids)
    assert len(stored) == 2
    assert stored in ({"w-owner", "w-lane-again"}, {"w-lane", "w-owner-again"})
    provenance = await loop.store.read_provenance(OVERLAY_ID)
    assert {p: e.actor for p, e in provenance.items()} == {
        PATH_OWNER: "actor-a",
        PATH_LANE: "actor-b",
    }


@pytest.mark.asyncio
async def test_concurrent_sets_positive_control_a_set_that_may_not_retry_is_refused_and_loses_nothing() -> (
    None
):
    """The check can see a lost value: with one attempt the loser is refused, and the store holds the winner's value only."""
    loop = Loopback(gate_first_reads=2)
    await loop.seed()
    first = loop.handler(max_attempts=1, write_ids=["w-owner"])
    second = loop.handler(max_attempts=1, write_ids=["w-lane"])

    results = await asyncio.gather(
        first.handle(_request("owner", "owner-b", "actor-a")),
        second.handle(_request("alarm_lane", "lane-b", "actor-b")),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, SettingWriteConflictError)]
    assert len(refused) == 1
    final = await loop.store.read_current(OVERLAY_ID)
    assert final is not None
    assert len(final["operator_paths"]) == 1
    assert len(loop.store.audit_write_ids) == 1
