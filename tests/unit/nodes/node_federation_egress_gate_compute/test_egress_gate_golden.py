# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The golden chain: well-formed records pass, deterministically, leaking nothing.

The shared synthetic policy of these tests: a closed value shape of a required
``command_id`` uuid, an optional ``reason_code`` slug and an optional bounded
``attempt``, the key bound to ``command_id``, one allowed ``message-id``
header, and the bus's three stamp headers refused.
"""

from __future__ import annotations

import base64
import hashlib
import random
import uuid

import pytest

from omnimarket.models.federation_egress.enum_egress_gate import (
    EnumEgressOutcome,
)
from omnimarket.models.federation_egress.model_egress_gate import (
    ModelEgressField,
    ModelEgressGateRequest,
    ModelEgressTopicPolicy,
    ModelHeaderRule,
    ModelWireHeader,
)
from omnimarket.nodes.node_federation_egress_gate_compute.handlers.handler_federation_egress_gate import (
    HandlerFederationEgressGate,
)

pytestmark = pytest.mark.unit

TOPIC = "example-tenant-a.commands"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
REASON_CODE_PATTERN = r"^[a-z-]{1,32}$"
MESSAGE_ID_PATTERN = r"^[0-9a-f-]{36}$"
COMMAND_ID = "0123456a-b2c3-4d5e-6f70-1234567890ab"
MESSAGE_ID = "1a2b3c4d-5e6f-0a1b-2c3d-3e4f5a6b7c8d"


def _policy() -> ModelEgressTopicPolicy:
    return ModelEgressTopicPolicy(
        fields=(
            ModelEgressField(
                name="command_id",
                kind="string",
                required=True,
                pattern=UUID_PATTERN,
                max_length=36,
            ),
            ModelEgressField(
                name="reason_code",
                kind="string",
                required=False,
                pattern=REASON_CODE_PATTERN,
                max_length=32,
            ),
            ModelEgressField(
                name="attempt",
                kind="integer",
                required=False,
                minimum=0,
                maximum=9,
            ),
        ),
        key_pattern=UUID_PATTERN,
        key_binding="command_id",
        header_allow_list=(
            ModelHeaderRule(name="message-id", pattern=MESSAGE_ID_PATTERN),
        ),
        max_headers=4,
        max_value_bytes=4096,
        max_value_depth=8,
        stamp_header_names=("origin-lane", "source-lane", "hops"),
        message_id_header="message-id",
    )


def _request(
    value: bytes,
    *,
    key: str | None,
    headers: tuple[tuple[str, str], ...] = (("message-id", MESSAGE_ID),),
) -> ModelEgressGateRequest:
    return ModelEgressGateRequest(
        topic=TOPIC,
        key_b64=(
            None if key is None else base64.b64encode(key.encode("utf-8")).decode()
        ),
        value_b64=base64.b64encode(value).decode(),
        headers=tuple(
            ModelWireHeader(name=header_name, value=header_value)
            for header_name, header_value in headers
        ),
        policy=_policy(),
    )


def test_a_well_formed_record_passes_with_digest_size_and_message_id() -> None:
    value = f'{{"command_id":"{COMMAND_ID}"}}'.encode()
    result = HandlerFederationEgressGate().handle(_request(value, key=COMMAND_ID))
    assert result.outcome is EnumEgressOutcome.PASS
    assert result.reason is None
    assert result.key_digest == hashlib.sha256(COMMAND_ID.encode()).hexdigest()
    assert result.message_id == MESSAGE_ID
    assert result.value_size == len(value)


def test_the_same_request_twice_gives_the_same_result() -> None:
    request = _request(
        f'{{"command_id":"{COMMAND_ID}","attempt":3}}'.encode(), key=COMMAND_ID
    )
    first = HandlerFederationEgressGate().handle(request)
    second = HandlerFederationEgressGate().handle(request)
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_fifty_generated_records_each_pass() -> None:
    rng = random.Random(7)
    handler = HandlerFederationEgressGate()
    for _ in range(50):
        command_id = str(uuid.UUID(int=rng.getrandbits(128)))
        message_id = str(uuid.UUID(int=rng.getrandbits(128)))
        value = f'{{"command_id":"{command_id}"}}'.encode()
        result = handler.handle(
            _request(value, key=command_id, headers=(("message-id", message_id),))
        )
        assert result.outcome is EnumEgressOutcome.PASS, command_id
        assert result.reason is None
        assert result.value_size == len(value)
        assert result.key_digest == hashlib.sha256(command_id.encode()).hexdigest()
        assert result.message_id == message_id


def test_the_result_never_carries_the_key_or_the_value_text() -> None:
    value = f'{{"command_id":"{COMMAND_ID}","reason_code":"ok"}}'.encode()
    result = HandlerFederationEgressGate().handle(_request(value, key=COMMAND_ID))
    assert result.outcome is EnumEgressOutcome.PASS
    dump = result.model_dump_json()
    assert COMMAND_ID not in dump
    assert value.decode() not in dump
