# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: the lab JWKS verifier accepts one token shape and refuses every other with 401 and nothing published."""

from __future__ import annotations

import pytest

from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    ISSUER,
    batch,
    make_app,
    mint,
    other_key,
    post,
    record,
)


async def test_claims_lab_jwks_accepts_the_lab_token() -> None:
    app, publisher = make_app()
    result = await post(app, batch(record()), token=mint())
    assert result.status == 202
    assert len(publisher.calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"key": other_key()}, "token-signature"),
        ({"audience": "kafka-broker"}, "token-audience"),
        ({"audience": ["gateway-attach", "kafka-broker"]}, "token-audience"),
        ({"issuer": ISSUER + "-other"}, "token-issuer"),
        ({"exp_in": -30}, "token-expired"),
        ({"kid": "unknown-kid"}, "token-unknown-kid"),
    ],
)
async def test_claims_lab_jwks_refuses(kwargs: dict[str, object], reason: str) -> None:
    app, publisher = make_app()
    result = await post(app, batch(record()), token=mint(**kwargs))  # type: ignore[arg-type]
    assert (result.status, result.body["error"]) == (401, reason)
    assert publisher.calls == []


async def test_claims_lab_jwks_refuses_a_missing_or_malformed_bearer() -> None:
    app, publisher = make_app()
    result = await post(app, batch(record()), token="not-a-jwt")
    assert (result.status, result.body["error"]) == (401, "token-malformed")
    assert publisher.calls == []
