# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24914: the writer matches the topics dispatch passes, namespaced or not, and ticks every tenant."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_RUN_CLOSED,
    TOPIC_SNAPSHOT,
    TOPIC_TICK,
)
from omnimarket.topic_namespace import (
    TOPIC_NAMESPACE_ENV_VAR,
    apply_topic_namespace,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    TENANT,
    MemoryStore,
    at,
    close_event,
    code_event,
    deliver,
    make_writer,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"


async def test_a_namespaced_source_topic_is_matched_and_the_snapshot_published_namespaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOPIC_NAMESPACE_ENV_VAR, "prepr1")
    store = MemoryStore()
    writer, published = make_writer(store)
    namespaced = apply_topic_namespace(TOPIC_CODE_TRY)
    assert namespaced != TOPIC_CODE_TRY
    await deliver(writer, namespaced, code_event(RUN, try_row(RUN, "t1")), at(2))
    await deliver(
        writer,
        apply_topic_namespace(TOPIC_RUN_CLOSED),
        close_event(RUN, (("t1", MODEL, "S"),)),
        at(3),
    )
    assert len(store.items) == 1
    assert published[-1][0] == apply_topic_namespace(TOPIC_SNAPSHOT)


async def test_a_namespaced_tick_republishes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOPIC_NAMESPACE_ENV_VAR, "prepr1")
    writer, published = make_writer(MemoryStore())
    await deliver(
        writer, apply_topic_namespace(TOPIC_TICK), {"now": at(50).isoformat()}, at(50)
    )
    assert [t for t, _ in published] == [apply_topic_namespace(TOPIC_SNAPSHOT)]


async def test_a_topic_outside_the_contract_is_still_refused() -> None:
    writer, _ = make_writer(MemoryStore())
    with pytest.raises(ValueError, match="not a source topic"):
        await deliver(writer, "onex.evt.other.thing.v1", {}, at(2))


async def test_a_tick_republishes_the_snapshot_of_every_tenant_the_writer_has_projected() -> (
    None
):
    store = MemoryStore()
    writer, published = make_writer(store)
    for tenant in (TENANT, OTHER_TENANT):
        await deliver(
            writer, TOPIC_CODE_TRY, code_event(RUN, try_row(RUN, "t1")), at(2), tenant
        )
        await deliver(
            writer,
            TOPIC_RUN_CLOSED,
            close_event(RUN, (("t1", MODEL, "S"),)),
            at(3),
            tenant,
        )
    published.clear()
    outcome = await deliver(writer, TOPIC_TICK, {"now": at(900).isoformat()}, at(900))
    assert outcome["tenants_published"] == 2
    assert sorted(s["tenant_id"] for _, s in published) == sorted(
        [TENANT, OTHER_TENANT]
    )
