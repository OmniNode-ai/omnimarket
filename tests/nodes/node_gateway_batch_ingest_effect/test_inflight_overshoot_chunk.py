# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC21: an overshoot chunk cannot be materialized past the reservation.

The runtime's bridge hands the door at most the remaining allowance plus one byte of the mount's `max_body_bytes` per
`receive`; the door reserves that bound plus headroom larger than the mount ceiling before it reads. These tests
measure the largest chunk the door's `receive` ever materializes against what was reserved, with the bridge's chunk
rule simulated. The same measurement against the real bridge at the head of omnibase_infra#6090 is in the PR's lab
comment (it needs a runtime that carries the body cap, which the locked omnibase-infra predates).
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_gateway_batch_ingest_effect.asgi import (
    BatchIngestApp,
    OverlayBindings,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.claims_jwks import LabJwksClaims
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    MOUNT_CEILING,
    PREFIX,
    FakePublisher,
    make_app,
    make_overlay,
    mint,
)

OVERLAY = make_overlay()
RESERVATION_CHUNKED = (
    OVERLAY.limits.max_body_bytes + OVERLAY.limits.chunk_headroom_bytes
)
BODY = b"x" * (4 * 1024 * 1024)


async def _post_understated(
    app: BatchIngestApp, *, ceiling: int | None
) -> tuple[int, list[int], int]:
    """An understated (chunked) 4 MiB body behind a bridge that applies the mount ceiling to each read."""
    delivered: list[int] = []
    reserved_during_read: list[int] = []
    position = 0

    async def receive() -> dict[str, Any]:
        nonlocal position
        remaining = len(BODY) - position
        take = (
            remaining if ceiling is None else min(remaining, ceiling + 1)
        )  # the bridge reads remaining allowance + 1
        position += take
        delivered.append(take)
        reserved_during_read.append(app.gate.reserved_bytes)
        return {
            "type": "http.request",
            "body": BODY[position - take : position],
            "more_body": position < len(BODY),
        }

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    headers = [(b"host", b"d"), (b"authorization", b"Bearer " + mint().encode())]
    await app(
        {"type": "http", "method": "POST", "path": PREFIX, "headers": headers},
        receive,
        send,
    )
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, delivered, max(reserved_during_read)


async def test_inflight_overshoot_chunk_never_exceeds_the_reservation_behind_a_bounded_mount() -> (
    None
):
    app, publisher = make_app(OVERLAY)
    status, delivered, reserved = await _post_understated(app, ceiling=MOUNT_CEILING)
    assert status == 413
    assert (
        reserved == RESERVATION_CHUNKED
    )  # reserved before the first read, not charged as read
    assert (
        max(delivered) <= reserved
    )  # the largest chunk the door materialized fits inside the reservation
    assert max(delivered) == MOUNT_CEILING + 1
    assert publisher.calls == []
    assert (app.gate.requests, app.gate.reserved_bytes) == (
        0,
        0,
    )


async def test_inflight_overshoot_chunk_an_unbounded_chunk_would_pass_the_reservation() -> (
    None
):
    """Negative control: with no mount ceiling the same measurement turns red, so the bound above is what holds."""
    app, _ = make_app(OVERLAY)
    _, delivered, reserved = await _post_understated(app, ceiling=None)
    assert max(delivered) > reserved


def test_inflight_overshoot_chunk_headroom_must_exceed_the_mount_ceiling() -> None:
    overlay = make_overlay(chunk_headroom_bytes=MOUNT_CEILING)
    with pytest.raises(ValueError, match="chunk_headroom_bytes"):
        BatchIngestApp(
            overlay=overlay,
            claims=LabJwksClaims(overlay.claims),
            bindings=OverlayBindings(overlay),
            publisher=FakePublisher(),
            path_prefix=PREFIX,
            mount_max_body_bytes=MOUNT_CEILING,
        )
