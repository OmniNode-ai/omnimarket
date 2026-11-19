# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 and AC4: the tenant is bound from the claim to the registry; a body or topic naming another tenant is refused."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    T1,
    T2,
    TOPIC_A,
    batch,
    make_app,
    mint,
    overlay_document,
    post,
    record,
)


async def test_tenant_binding_refuses_t2_signed_with_slug_a() -> None:
    app, publisher = make_app()
    result = await post(app, batch(record()), token=mint(tenant_id=T2, slug="a"))
    assert (result.status, result.body["error"]) == (403, "tenant-binding-mismatch")
    assert publisher.calls == []


@pytest.mark.parametrize(
    "claim",
    [
        {"tenant_id": "99999999-9999-4999-8999-999999999999", "slug": "a"},
        {"tenant_id": T1, "slug": "b"},
        {"tenant_id": T1, "slug": "A"},
    ],
)
async def test_tenant_binding_refuses_unknown_id_and_slug_mismatch(
    claim: dict[str, str],
) -> None:
    app, publisher = make_app()
    result = await post(app, batch(record()), token=mint(**claim))
    assert (result.status, result.body["error"]) == (403, "tenant-binding-mismatch")
    assert publisher.calls == []


def test_tenant_binding_load_refuses_repeated_id_or_slug() -> None:
    repeated_id = copy.deepcopy(overlay_document())
    repeated_id["tenants"][1]["tenant_id"] = T1
    repeated_slug = copy.deepcopy(overlay_document())
    repeated_slug["tenants"][1]["tenant_slug"] = "a"
    for document in (repeated_id, repeated_slug):
        with pytest.raises(ValidationError):
            ModelBatchIngestOverlay.model_validate(document)


async def test_tenant_from_claim_only_body_naming_a_tenant_is_refused() -> None:
    app, publisher = make_app()
    for body in (
        b'{"tenant":"b","records":[]}',
        b'{"tenant_id":"22222222-2222-4222-8222-222222222222","records":[]}',
    ):
        result = await post(app, body)
        assert (result.status, result.body["error"]) == (422, "unknown-field")
    named = record()
    named["tenant_slug"] = "b"
    result = await post(app, batch(named))
    assert (result.status, result.body["error"], result.body["index"]) == (
        422,
        "unknown-field",
        0,
    )
    assert publisher.calls == []


async def test_tenant_prefix_of_another_tenant_is_refused() -> None:
    app, publisher = make_app()
    result = await post(app, batch(record(wire_topic="tenant-b." + TOPIC_A)))
    assert (result.status, result.body["error"]) == (422, "topic-tenant-mismatch")
    assert publisher.calls == []
