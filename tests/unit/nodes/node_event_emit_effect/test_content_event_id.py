# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Byte-level cross-leg content identity conformance fixture."""

from __future__ import annotations

from omnimarket.nodes.node_event_emit_effect.enrichment import (
    canonical_content_event_bytes,
    content_event_id,
)

TOPIC = "onex.evt.omniclaude.tool-executed.v1"
EXPECTED_BYTES = (
    b"onex.evt.omniclaude.tool-executed.v1\n"
    b'{"a":{"empty":null,"number":1},"z":["\\u03b1",'
    b'{"a":"e\\u0301","b":2}]}'
)
EXPECTED_ID = "038a80439a761a25394282d9a97dad9a6d8a86303d416f346c937f62773cb871"


def test_content_identity_uses_frozen_canonical_bytes_for_unicode_nested_payload() -> (
    None
):
    payload = {
        "z": ["\u03b1", {"b": 2, "a": "e\u0301"}],
        "a": {"number": 1, "empty": None},
    }

    assert canonical_content_event_bytes(TOPIC, payload) == EXPECTED_BYTES
    assert content_event_id(TOPIC, payload) == EXPECTED_ID


def test_content_identity_is_stable_across_field_order_and_replay() -> None:
    first = {
        "z": ["\u03b1", {"b": 2, "a": "e\u0301"}],
        "a": {"number": 1, "empty": None},
    }
    replay = {
        "a": {"empty": None, "number": 1},
        "z": ["\u03b1", {"a": "e\u0301", "b": 2}],
    }

    assert content_event_id(TOPIC, first) == EXPECTED_ID
    assert content_event_id(TOPIC, replay) == EXPECTED_ID
