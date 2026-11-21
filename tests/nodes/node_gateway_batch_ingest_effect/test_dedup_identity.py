# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC18: one derivation of the event id, covering topic, key, declared identity headers and value."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_gateway_batch_ingest_effect.event_identity import (
    IDENTITY_HEADER_NAMES,
    derive_event_id,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    TOPIC_A,
    b64,
    batch,
    make_app,
    overlay_document,
    post,
    record,
)


async def test_dedup_identity_key_and_identity_header_differences_carry_four_distinct_ids() -> (
    None
):
    header = IDENTITY_HEADER_NAMES[0]
    records = [
        record(value=b"v"),
        record(value=b"v", key=b"k"),
        record(value=b"v", headers={header: "one"}),
        record(value=b"v", headers={header: "two"}),
    ]
    assert len({r["event_id"] for r in records}) == 4
    app, publisher = make_app()
    result = await post(app, batch(*records))
    assert (result.status, result.body) == (202, {"published": 4})
    assert len({c.event_id for c in publisher.calls}) == 4


async def test_dedup_identity_an_id_from_topic_and_value_only_is_refused() -> None:
    old_style = derive_event_id(TOPIC_A, None, {}, b"v")
    keyed = record(value=b"v", key=b"k")
    keyed["event_id"] = old_style  # derived without the key
    app, publisher = make_app()
    result = await post(app, batch(keyed))
    assert (result.status, result.body["error"]) == (422, "event-id-mismatch")
    assert publisher.calls == []


async def test_dedup_identity_wire_and_canonical_topic_forms_carry_one_id() -> None:
    canonical = record(value=b"v")
    wire = record(value=b"v", wire_topic="tenant-a." + TOPIC_A)
    assert canonical["event_id"] == wire["event_id"]
    app, _ = make_app()
    assert (await post(app, batch(canonical, wire))).status == 202


def test_dedup_identity_derivation_is_domain_separated_from_a_plain_hash() -> None:
    import hashlib

    plain = hashlib.sha256(TOPIC_A.encode() + b"\n" + b"v").hexdigest()
    assert derive_event_id(TOPIC_A, None, {}, b"v") != plain
    assert derive_event_id(TOPIC_A, b"", {}, b"v") != derive_event_id(
        TOPIC_A, None, {}, b"v"
    )
    assert b64(b"") == ""


def test_dedup_identity_the_overlay_refuses_an_identity_key() -> None:
    document = copy.deepcopy(overlay_document())
    document["identity"] = {"headers": ["x"]}
    with pytest.raises(ValidationError):
        ModelBatchIngestOverlay.model_validate(document)
