# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC9: in-flight request cap, byte reservation, total upload deadline, release on every exit, load inequalities."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_gateway_batch_ingest_effect.ingress import InflightGate
from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelPublishOutcome,
    ModelWireRecord,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    MOUNT_CEILING,
    FakePublisher,
    batch,
    make_app,
    make_overlay,
    overlay_document,
    post,
    record,
)

RESERVATION_CHUNKED = 16384 + MOUNT_CEILING + 1


async def test_inflight_caps_request_cap_refuses_the_third_concurrent_request() -> None:
    app, _ = make_app()
    release = asyncio.Event()

    async def block() -> None:
        await release.wait()

    stalled = [
        asyncio.create_task(
            post(
                app, chunks=[b"{"], content_length=None, stall=True, receive_hook=block
            )
        )
        for _ in range(2)
    ]
    await asyncio.sleep(0.05)
    assert app.gate.requests == 2
    third = await post(app, batch(record()))
    assert (third.status, third.body["error"]) == (503, "inflight-exceeded")
    release.set()
    await asyncio.gather(*stalled)
    assert app.gate.requests == 0


async def test_inflight_caps_byte_reservation_refuses_when_bytes_are_exhausted() -> (
    None
):
    overlay = make_overlay(
        max_inflight_bytes=2 * RESERVATION_CHUNKED - 1, max_inflight_requests=10
    )
    app, _ = make_app(overlay)
    release = asyncio.Event()

    async def block() -> None:
        await release.wait()

    first = asyncio.create_task(
        post(app, chunks=[b"{"], content_length=None, stall=True, receive_hook=block)
    )
    await asyncio.sleep(0.05)
    assert (
        app.gate.reserved_bytes == RESERVATION_CHUNKED
    )  # a chunked request reserves the cap
    second = await post(app, chunks=[b"{}"], content_length=None)
    assert (second.status, second.body["error"]) == (503, "inflight-exceeded")
    small = await post(
        app, batch(record())
    )  # declared length: reserves its length only
    assert small.status == 202
    release.set()
    await first


async def test_inflight_caps_bytes_are_reserved_not_charged_as_read() -> None:
    app, _ = make_app()
    seen: list[int] = []

    async def observe() -> None:
        seen.append(app.gate.reserved_bytes)

    await post(app, chunks=[b"{", b"}"], content_length=None, receive_hook=observe)
    assert set(seen) == {RESERVATION_CHUNKED}


async def test_inflight_caps_total_upload_deadline_beats_a_trickle() -> None:
    app, _ = make_app(
        make_overlay(read_idle_timeout_s=0.2, total_upload_deadline_s=0.4)
    )

    async def tick() -> None:
        await asyncio.sleep(0.15)  # inside the idle timeout every time

    started = asyncio.get_running_loop().time()
    result = await post(app, chunks=[b" "] * 50, content_length=None, receive_hook=tick)
    assert result.status == 408
    assert asyncio.get_running_loop().time() - started < 1.0
    assert app.gate.requests == 0


class _Raising(FakePublisher):
    async def publish(self, record: ModelWireRecord) -> ModelPublishOutcome:
        raise RuntimeError("boom")


@pytest.mark.parametrize(
    "exit_kind",
    ["normal", "422", "408", "401", "handler-raise", "cancel", "disconnect"],
)
async def test_inflight_caps_release_on_every_exit(exit_kind: str) -> None:
    app, _ = (
        make_app(publisher=_Raising(message_max_bytes=4096))
        if exit_kind == "handler-raise"
        else make_app()
    )
    if exit_kind == "normal":
        assert (await post(app, batch(record()))).status == 202
    elif exit_kind == "422":
        assert (await post(app, batch(record(wire_topic="nope")))).status == 422
    elif exit_kind == "408":
        assert (
            await post(app, chunks=[b"{"], content_length=None, stall=True)
        ).status == 408
    elif exit_kind == "401":
        assert (await post(app, batch(record()), token="x")).status == 401
    elif exit_kind == "handler-raise":
        assert (
            await post(app, batch(record()))
        ).status == 503  # the raise is a refused publish
    elif exit_kind == "cancel":
        task = asyncio.create_task(
            post(app, chunks=[b"{"], content_length=None, stall=True)
        )
        await asyncio.sleep(0.05)
        assert app.gate.requests == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await post(app, chunks=[b"{"], content_length=None, disconnect=True)
        assert result.status == 400  # the client went away mid-upload
    assert (app.gate.requests, app.gate.reserved_bytes) == (0, 0)


def test_inflight_caps_load_inequalities() -> None:
    for limits in (
        {"max_inflight_bytes": 16384},
        {"total_upload_deadline_s": 0.1, "read_idle_timeout_s": 0.2},
    ):
        document = overlay_document(**limits)
        with pytest.raises(ValidationError):
            ModelBatchIngestOverlay.model_validate(document)


def test_inflight_caps_counters_hold_no_record_or_event_id() -> None:
    gate = InflightGate(2, 100)
    assert gate.try_reserve(10)
    assert {k for k, v in vars(gate).items() if not isinstance(v, int)} == set()
