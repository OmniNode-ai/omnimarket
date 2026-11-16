# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24914: identity from the identity fields only; lane stamps out of the tie-break; providers and tenants refused."""

from __future__ import annotations

import pytest

from omnimarket.models.eval_item.model_eval_item_scored import event_id_of
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    TENANT,
    MemoryStore,
    at,
    code_event,
    deliver,
    judged_event,
    make_writer,
    overlay,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"
CALL = "00000000-0000-0000-0000-00000000c001"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"


async def test_the_item_key_is_the_uuid5_of_the_identity_fields_and_no_lane_stamp() -> (
    None
):
    keys = set()
    for host, revision in (("h202", "rev-1"), ("h101", "rev-2")):
        store = MemoryStore()
        writer, _ = make_writer(
            store, overlay_=overlay(host=host, install_revision=revision)
        )
        await deliver(
            writer, TOPIC_CODE_TRY, code_event(RUN, try_row(RUN, "t1")), at(5)
        )
        ((_, key),) = store.items
        keys.add(key)
    expected = event_id_of(
        scorer="contract_checks",
        run_id=RUN,
        task_id="t1",
        engine="local",
        model_id=MODEL,
        context_arm="off",
        replicate=0,
        try_ordinal=1,
    )
    assert keys == {str(expected)}


async def test_a_replay_after_a_redeploy_rewrites_no_row_in_either_order() -> None:
    """The same event delivered again by a runtime with new lane stamps changes nothing."""
    payload = code_event(RUN, try_row(RUN, "t1"))
    old = overlay(host="h202", install_revision="rev-1")
    new = overlay(host="h101", install_revision="rev-2")
    for first, second in ((old, new), (new, old)):
        store = MemoryStore()
        await deliver(
            make_writer(store, overlay_=first)[0], TOPIC_CODE_TRY, payload, at(5)
        )
        outcome = await deliver(
            make_writer(store, overlay_=second)[0], TOPIC_CODE_TRY, payload, at(5)
        )
        (row,) = store.items.values()
        assert (row.host, row.install_revision) == (first.host, first.install_revision)
        assert outcome["items_written"] == 0


async def test_a_judged_replay_after_a_redeploy_rewrites_no_row() -> None:
    payload = judged_event(CALL)
    store = MemoryStore()
    old = overlay(host="h202", install_revision="rev-1")
    new = overlay(host="h101", install_revision="rev-2")
    await deliver(make_writer(store, overlay_=new)[0], TOPIC_JUDGED, payload, at(5))
    outcome = await deliver(
        make_writer(store, overlay_=old)[0], TOPIC_JUDGED, payload, at(5)
    )
    (row,) = store.items.values()
    assert row.host == "h101"
    assert outcome["items_written"] == 0


@pytest.mark.parametrize("provider", ["gemini", "", "somebody-new"])
async def test_an_unknown_provider_is_refused_like_an_unmapped_tier(
    provider: str,
) -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    row = try_row(RUN, "t1", provider=provider)
    outcome = await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, row), at(5))
    assert [r["reason"] for r in outcome["refused"]] == ["unmapped_provider"]
    assert outcome["dlq_routed"] == 1
    assert store.items == {}


@pytest.mark.parametrize(
    ("provider", "engine"),
    [("local", "local"), ("glm", "glm"), ("codex", "codex"), ("claude", "claude")],
)
async def test_a_known_provider_keeps_its_engine(provider: str, engine: str) -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    row = try_row(RUN, "t1", provider=provider)
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, row), at(5))
    (item,) = store.items.values()
    assert item.engine.value == engine


async def test_a_judged_payload_tenant_that_differs_from_the_envelope_is_refused() -> (
    None
):
    store = MemoryStore()
    writer, published = make_writer(store)
    outcome = await deliver(
        writer, TOPIC_JUDGED, judged_event(CALL), at(5), tenant=OTHER_TENANT
    )
    assert [r["reason"] for r in outcome["refused"]] == ["tenant_mismatch"]
    assert outcome["dlq_routed"] == 1
    assert store.items == {}
    assert all("projection.eval-results" not in topic for topic, _ in published)


async def test_a_judged_payload_tenant_equal_to_the_envelope_is_stored_under_it() -> (
    None
):
    store = MemoryStore()
    writer, _ = make_writer(store)
    await deliver(writer, TOPIC_JUDGED, judged_event(CALL), at(5), tenant=TENANT)
    (item,) = store.items.values()
    assert item.tenant_id == TENANT
