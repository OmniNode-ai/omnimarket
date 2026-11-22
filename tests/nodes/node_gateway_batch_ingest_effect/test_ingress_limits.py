# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC8: ingress bounds, with an ASGI harness that spies on `receive`."""

from __future__ import annotations

from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    batch,
    make_app,
    make_overlay,
    mint,
    post,
    record,
)


async def test_ingress_limits_over_cap_content_length_is_413_with_receive_never_called() -> (
    None
):
    app, publisher = make_app()
    result = await post(app, b"x" * 16385)
    assert (result.status, result.receive_calls) == (413, 0)
    assert publisher.calls == []


async def test_ingress_limits_chunked_body_stops_at_the_cap_plus_one_chunk() -> None:
    app, publisher = make_app()
    chunks = [b"x" * 6000] * 10
    result = await post(app, chunks=chunks, content_length=None)
    assert result.status == 413
    assert (
        result.receive_calls == 3
    )  # 18000 > 16384 on the third chunk; the rest is never read
    assert sum(result.chunk_sizes) == 18000
    assert publisher.calls == []


async def test_ingress_limits_understated_body_stops_at_the_declared_length_plus_one_chunk() -> (
    None
):
    app, _ = make_app()
    result = await post(
        app, chunks=[b"x" * 100, b"x" * 100, b"x" * 100], content_length=b"50"
    )
    assert (result.status, result.receive_calls) == (413, 1)


async def test_ingress_limits_stalled_body_is_408() -> None:
    app, publisher = make_app()
    result = await post(app, chunks=[b'{"records":'], content_length=None, stall=True)
    assert result.status == 408
    assert publisher.calls == []


async def test_ingress_limits_oversize_headers_are_431_unread() -> None:
    app, _ = make_app()
    many = [(f"x-extra-{i}".encode(), b"1") for i in range(20)]
    for headers, reason in (
        (many, "too-many-headers"),
        ([(b"x-forwarded-for", b"v" * 600)], "header-too-large"),
    ):
        result = await post(app, batch(record()), headers=headers)
        assert (result.status, result.body["error"], result.receive_calls) == (
            431,
            reason,
            0,
        )
    token = mint() + "x" * 1100
    result = await post(app, batch(record()), token=token)
    assert (result.status, result.body["error"], result.receive_calls) == (
        431,
        "authorization-too-large",
        0,
    )


async def test_ingress_limits_malformed_content_length_is_400() -> None:
    app, _ = make_app()
    for raw in (b"abc", b"-1", b"1.5", b"0x10", b"", b"99999999999999999999"):
        result = await post(app, b"{}", content_length=raw)
        assert result.status in (400, 413), raw
        assert result.receive_calls == 0
    assert (await post(app, b"{}", content_length=b"abc")).status == 400


async def test_ingress_limits_json_nested_past_max_depth_is_refused() -> None:
    app, publisher = make_app(make_overlay(max_json_depth=4))
    deep = b'{"records":' + b"[" * 10 + b"]" * 10 + b"}"
    result = await post(app, deep)
    assert (result.status, result.body["error"]) == (422, "json-too-deep")
    assert publisher.calls == []
    strings = b'{"records":[{"topic":"' + b"[" * 50 + b'"}]}'
    assert (await post(app, strings)).body["error"] != "json-too-deep"


async def test_ingress_limits_not_json_is_400() -> None:
    app, _ = make_app()
    result = await post(app, b"{not json")
    assert (result.status, result.body["error"]) == (400, "body-not-json")


async def test_ingress_limits_path_and_method() -> None:
    app, _ = make_app()
    assert (await post(app, b"{}", path="/v1/gateway/batch-ingest/x")).status == 404
    assert (await post(app, b"{}", method="GET")).status == 405
