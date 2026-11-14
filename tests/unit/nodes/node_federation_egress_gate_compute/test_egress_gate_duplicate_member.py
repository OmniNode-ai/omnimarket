# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duplicate-member scan, pinned on the wire bytes it must read.

Every fixture is a RAW BYTES literal: ``json.dumps`` of a dict can never spell
two pairs of one name, so the fixtures are written exactly as they would
arrive. Each test runs both arms — the gate as shipped refuses the record
``duplicate-member``, and the same record passes only when the scan is
monkeypatched away — so a red in the first arm is the scan's.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_federation_egress_gate_compute.handlers import (
    handler_federation_egress_gate,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.enum_egress_gate import (
    EnumEgressOutcome,
    EnumEgressRefusal,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.model_egress_gate import (
    ModelEgressField,
    ModelEgressGateRequest,
    ModelEgressTopicPolicy,
    ModelHeaderRule,
    ModelWireHeader,
)

pytestmark = pytest.mark.unit

TOPIC = "example-tenant-a.commands"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
REASON_CODE_PATTERN = r"^[a-z-]{1,32}$"
MESSAGE_ID_PATTERN = r"^[0-9a-f-]{36}$"
MESSAGE_ID = "1a2b3c4d-5e6f-0a1b-2c3d-3e4f5a6b7c8d"

U = "0123456a-b2c3-4d5e-6f70-1234567890ab"
U_BYTES = U.encode("ascii")
MARK = "synthetic-customer-marker"
MARK_BYTES = MARK.encode("ascii")

# (a) the forbidden value FIRST, the valid one LAST: last-wins json.loads
# would accept this record, so only the scan refuses it.
FIXTURE_FORBIDDEN_FIRST = b'{"command_id":"not-a-uuid","command_id":"' + U_BYTES + b'"}'
# (b) the valid value FIRST, the forbidden one LAST: a parser keeping the
# FIRST of a repeated name would accept this record.
FIXTURE_VALID_FIRST = b'{"command_id":"' + U_BYTES + b'","command_id":"not-a-uuid"}'
# (d) the second name is the first's ``_`` escape spelling: the decoder
# resolves it to the same member name, so it is the same duplicate.
FIXTURE_ESCAPE_SPELLING = (
    b'{"command_id":"' + U_BYTES + b'","command\\u005fid":"' + U_BYTES + b'"}'
)
# (e) a duplicated name that is not the bound field: the last value, the
# marker, would pass the closed model untouched.
FIXTURE_OTHER_NAME = (
    b'{"reason_code":"ok","command_id":"'
    + U_BYTES
    + b'","reason_code":"'
    + MARK_BYTES
    + b'"}'
)


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


def _request(value: bytes, *, key: str) -> ModelEgressGateRequest:
    return ModelEgressGateRequest(
        topic=TOPIC,
        key_b64=base64.b64encode(key.encode("utf-8")).decode(),
        value_b64=base64.b64encode(value).decode(),
        headers=(ModelWireHeader(name="message-id", value=MESSAGE_ID),),
        policy=_policy(),
    )


def _last_wins(value: bytes, *, max_depth: int) -> tuple[dict[str, Any] | None, bool]:
    """A deliberately blind parser: plain ``json.loads``, never a duplicate."""
    return json.loads(value), False


def _first_wins(value: bytes, *, max_depth: int) -> tuple[dict[str, Any] | None, bool]:
    """A deliberately blind parser keeping the FIRST of a repeated name."""

    def keep_first(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        picked: dict[str, Any] = {}
        for name, item in pairs:
            if name not in picked:
                picked[name] = item
        return picked

    return json.loads(value, object_pairs_hook=keep_first), False


def _assert_refused_duplicate_member(request: ModelEgressGateRequest) -> None:
    result = handler_federation_egress_gate.HandlerFederationEgressGate().handle(
        request
    )
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.DUPLICATE_MEMBER


def _assert_passes(request: ModelEgressGateRequest) -> None:
    result = handler_federation_egress_gate.HandlerFederationEgressGate().handle(
        request
    )
    assert result.outcome is EnumEgressOutcome.PASS
    assert result.reason is None


def test_forbidden_first_valid_last_is_refused_only_by_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert FIXTURE_FORBIDDEN_FIRST.count(b'"command_id"') == 2
    _assert_refused_duplicate_member(_request(FIXTURE_FORBIDDEN_FIRST, key=U))
    monkeypatch.setattr(
        handler_federation_egress_gate,
        "parse_value_refusing_duplicates",
        _last_wins,
    )
    _assert_passes(_request(FIXTURE_FORBIDDEN_FIRST, key=U))


def test_valid_first_forbidden_last_is_refused_only_by_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert FIXTURE_VALID_FIRST.count(b'"command_id"') == 2
    _assert_refused_duplicate_member(_request(FIXTURE_VALID_FIRST, key=U))
    monkeypatch.setattr(
        handler_federation_egress_gate,
        "parse_value_refusing_duplicates",
        _first_wins,
    )
    _assert_passes(_request(FIXTURE_VALID_FIRST, key=U))


def test_escape_spelling_of_one_name_is_refused_only_by_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert b"command\\u005fid" in FIXTURE_ESCAPE_SPELLING
    assert FIXTURE_ESCAPE_SPELLING.count(b'"command_id"') == 1
    _assert_refused_duplicate_member(_request(FIXTURE_ESCAPE_SPELLING, key=U))
    monkeypatch.setattr(
        handler_federation_egress_gate,
        "parse_value_refusing_duplicates",
        _last_wins,
    )
    _assert_passes(_request(FIXTURE_ESCAPE_SPELLING, key=U))


def test_a_duplicated_name_that_is_not_the_bound_field_is_refused_only_by_the_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert FIXTURE_OTHER_NAME.count(b'"reason_code"') == 2
    assert MARK_BYTES in FIXTURE_OTHER_NAME
    request = _request(FIXTURE_OTHER_NAME, key=U)
    _assert_refused_duplicate_member(request)
    assert MARK_BYTES in base64.b64decode(request.value_b64)
    monkeypatch.setattr(
        handler_federation_egress_gate,
        "parse_value_refusing_duplicates",
        _last_wins,
    )
    _assert_passes(request)


def test_a_duplicate_inside_a_nested_object_is_seen_at_any_depth() -> None:
    # The closed model of the policy above allows nothing nested, so the
    # nested duplicates are asserted on the parser itself, at the policy's
    # own depth bound.
    policy = _policy()
    nested_object, object_duplicate = (
        handler_federation_egress_gate.parse_value_refusing_duplicates(
            b'{"a":{"b":1,"b":2}}', max_depth=policy.max_value_depth
        )
    )
    assert nested_object is not None
    assert object_duplicate is True
    nested_array, array_duplicate = (
        handler_federation_egress_gate.parse_value_refusing_duplicates(
            b'{"a":[{"b":1,"b":2}]}', max_depth=policy.max_value_depth
        )
    )
    assert nested_array is not None
    assert array_duplicate is True


def test_a_passing_records_value_bytes_are_never_altered() -> None:
    raw = b'{"command_id":"' + U_BYTES + b'"}'
    request = _request(raw, key=U)
    _assert_passes(request)
    assert base64.b64decode(request.value_b64) == raw
    source = Path(handler_federation_egress_gate.__file__).read_text()
    assert "json.dumps" not in source
