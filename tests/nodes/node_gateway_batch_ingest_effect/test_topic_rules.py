# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC6: topic normalization and the exact allowlist; overlay topics may not carry the tenant prefix."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.topic_rules import (
    TopicRefusedError,
    normalize_topic,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    TOPIC_A,
    TOPIC_B,
    batch,
    make_app,
    overlay_document,
    post,
    record,
)

ALLOWED = {TOPIC_A, TOPIC_B}


def _normalize(raw: object) -> str:
    return normalize_topic(
        raw, slug="a", max_topic_chars=100, max_wire_topic_chars=140, allowed=ALLOWED
    )


@pytest.mark.parametrize(
    "raw", [TOPIC_A, "tenant-a." + TOPIC_A, TOPIC_B, "tenant-a." + TOPIC_B]
)
def test_topic_rules_accept_canonical_and_own_wire_form(raw: str) -> None:
    assert _normalize(raw) in ALLOWED


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (TOPIC_A + "x", "topic-not-allowed"),
        (TOPIC_A.upper(), "topic-not-allowed"),
        ("onex.evt.omnimarket.pool-usage-verdict-evaluated.v2", "topic-not-allowed"),
        ("tenant-a.tenant-a." + TOPIC_A, "topic-nested-prefix"),
        ("tenant-b." + TOPIC_A, "topic-tenant-mismatch"),
        ("tenant-" + TOPIC_A, "topic-tenant-mismatch"),
        ("Tenant-a." + TOPIC_A, "topic-not-allowed"),
        ("tenant-a" + TOPIC_A, "topic-tenant-mismatch"),
        (TOPIC_A.replace(".", "%2e"), "topic-encoded"),
        (TOPIC_A + "\n", "topic-encoded"),
        (TOPIC_A + "\x00", "topic-encoded"),
        (
            TOPIC_A.replace("omnimarket", "omnimarket\u0435"[:-1] + "\u0435"),
            "topic-encoded",
        ),  # Cyrillic e
        (" " + TOPIC_A, "topic-encoded"),
        ("", "topic-encoded"),
        ("x" * 141, "topic-too-long"),
        (7, "topic-not-string"),
        (None, "topic-not-string"),
        (["a"], "topic-not-string"),
    ],
)
def test_topic_rules_refuse_with_the_named_reason(raw: object, reason: str) -> None:
    with pytest.raises(TopicRefusedError) as caught:
        _normalize(raw)
    assert caught.value.reason == reason


async def test_topic_rules_publish_both_forms_on_the_one_wire_topic() -> None:
    app, publisher = make_app()
    result = await post(
        app, batch(record(), record(wire_topic="tenant-a." + TOPIC_A, value=b"2"))
    )
    assert result.status == 202
    assert {c.topic for c in publisher.calls} == {"tenant-a." + TOPIC_A}


async def test_topic_rules_refusal_publishes_nothing() -> None:
    app, publisher = make_app()
    result = await post(app, batch(record(), record(wire_topic=TOPIC_A + "x")))
    assert (result.status, result.body["error"], result.body["index"]) == (
        422,
        "topic-not-allowed",
        1,
    )
    assert publisher.calls == []


def test_overlay_topics_refuse_a_tenant_prefixed_topic() -> None:
    document = copy.deepcopy(overlay_document())
    document["topics"][0]["topic"] = "tenant-a." + TOPIC_A
    with pytest.raises(ValidationError):
        ModelBatchIngestOverlay.model_validate(document)
