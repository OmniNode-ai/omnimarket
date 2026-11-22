# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3: a retired tenant's slug is never reused, at admission and at load."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    T1,
    T2,
    batch,
    make_app,
    mint,
    overlay_document,
    post,
    record,
)


def _retired_document() -> dict[str, Any]:
    document = copy.deepcopy(overlay_document())
    document["tenants"][0]["status"] = "retired"
    return document


async def test_slug_reuse_retired_tenant_token_is_refused() -> None:
    overlay = ModelBatchIngestOverlay.model_validate(_retired_document())
    app, publisher = make_app(overlay)
    result = await post(app, batch(record()), token=mint(tenant_id=T1, slug="a"))
    assert (result.status, result.body["error"]) == (403, "tenant-retired")
    assert publisher.calls == []


async def test_slug_reuse_later_tenant_with_the_retired_slug_is_refused() -> None:
    overlay = ModelBatchIngestOverlay.model_validate(_retired_document())
    app, publisher = make_app(overlay)
    result = await post(app, batch(record()), token=mint(tenant_id=T2, slug="a"))
    assert (result.status, result.body["error"]) == (403, "tenant-binding-mismatch")
    assert publisher.calls == []


def test_slug_reuse_load_refuses_a_reused_retired_slug_or_rebound_id() -> None:
    reused = _retired_document()
    reused["tenants"][1]["tenant_slug"] = "a"
    rebound = _retired_document()
    rebound["tenants"].append({"tenant_id": T1, "tenant_slug": "c", "status": "active"})
    for document in (reused, rebound):
        with pytest.raises(ValidationError):
            ModelBatchIngestOverlay.model_validate(document)
