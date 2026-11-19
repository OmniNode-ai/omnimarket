# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC19 (ASGI side): declared header names pass at their maxima, undeclared names are 431 with `receive` never called.

The real-client loopback test is test_header_budget_real_client.py, which needs unmerged code and is pending.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    CLIENT_HEADER_SET,
    ModelBatchIngestOverlay,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    batch,
    make_app,
    make_overlay,
    overlay_document,
    post,
    record,
)


async def test_header_budget_maximum_header_request_is_202() -> None:
    overlay = make_overlay()
    declared = len(CLIENT_HEADER_SET) + len(overlay.proxy_headers)
    app, publisher = make_app(make_overlay(max_header_count=declared))
    extra = [
        (b"x-forwarded-for", b"f" * 64),
        (b"x-request-id", b"r" * 32),
        (b"user-agent", b"u"),
        (b"accept", b"*/*"),
        (b"accept-encoding", b"identity"),
        (b"connection", b"close"),
        (b"idempotency-key", b"k" * 40),
    ]
    result = await post(app, batch(record()), headers=extra)
    assert result.status == 202
    stamped = dict(publisher.calls[0].headers)
    assert not {"x-forwarded-for", "x-request-id", "user-agent"} & set(stamped)


async def test_header_budget_proxy_header_over_its_maximum_is_431() -> None:
    app, _ = make_app()
    result = await post(app, batch(record()), headers=[(b"x-request-id", b"r" * 33)])
    assert (result.status, result.body["error"]) == (431, "header-too-large")


async def test_header_budget_undeclared_name_is_431_with_receive_never_called() -> None:
    app, publisher = make_app()
    result = await post(app, batch(record()), headers=[(b"x-surprise", b"1")])
    assert (result.status, result.body["error"], result.receive_calls) == (
        431,
        "header-not-declared",
        0,
    )
    assert publisher.calls == []


def test_header_budget_load_refuses_a_count_below_the_declared_names() -> None:
    document = overlay_document()
    document["limits"]["max_header_count"] = len(CLIENT_HEADER_SET) + 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        ModelBatchIngestOverlay.model_validate(document)
