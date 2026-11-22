# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC13: the egress gate runs on every record."""

from __future__ import annotations

import json

import pytest

from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    b64,
    batch,
    make_app,
    post,
    record,
)


def _raw(record_json: str) -> bytes:
    return ('{"records":[' + record_json + "]}").encode()


async def test_egress_duplicate_member_names_are_refused() -> None:
    app, publisher = make_app()
    good = record(headers={"x-a": "1"})
    members = json.dumps(good)
    in_headers = members.replace('"x-a": "1"', '"x-a": "1", "x-a": "2"')
    assert in_headers != members
    in_record = members[:-1] + ', "envelope_id": "env-2"}'
    for body in (_raw(in_headers), _raw(in_record)):
        result = await post(app, body)
        assert (result.status, result.body["error"], result.body["index"]) == (
            422,
            "egress-duplicate-member-name",
            0,
        )
    names = await post(app, batch({**good, "headers": {"X-A": "1", "x-a": "2"}}))
    assert names.body["error"] == "egress-duplicate-member-name"
    assert publisher.calls == []


async def test_egress_oversize_value_is_refused_with_the_gates_own_reason() -> None:
    app, publisher = make_app()
    big = record(value=b"v" * 4001)
    result = await post(app, batch(big))
    assert (result.status, result.body["error"]) == (422, "egress-value-too-large")
    assert publisher.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "onex-tenant-id",
        "Onex-Tenant-Id",
        "onex-tenant-slug",
        "onex-ingest-door",
    ],
)
async def test_egress_client_sent_stamp_header_is_refused(name: str) -> None:
    app, publisher = make_app()
    result = await post(app, batch(record(headers={name: "forged"})))
    assert (result.status, result.body["error"]) == (422, "egress-client-stamp-header")
    assert publisher.calls == []


async def test_egress_client_message_id_is_the_identity_header_and_the_door_replaces_it() -> (
    None
):
    app, publisher = make_app()
    sent = record(headers={"message-id": "client-chosen"})
    result = await post(app, batch(sent))
    assert (result.status, result.body) == (202, {"published": 1})
    [produced] = publisher.calls
    assert [n for n, _ in produced.headers].count("message-id") == 1
    assert dict(produced.headers)["message-id"] == sent["event_id"].encode()


async def test_egress_gate_runs_on_every_record_not_only_the_first() -> None:
    app, publisher = make_app()
    result = await post(
        app,
        batch(record(), record(value=b"2"), record(headers={"onex-tenant-id": "x"})),
    )
    assert (result.body["error"], result.body["index"]) == (
        "egress-client-stamp-header",
        2,
    )
    assert publisher.calls == []
    assert b64(b"x")
