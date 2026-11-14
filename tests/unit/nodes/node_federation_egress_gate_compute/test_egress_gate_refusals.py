# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The error chain: one refusal per named reason, each with a passing control.

Every case asserts the exact refusal reason, that the same request with only
the defect removed passes, and that the refusal answer carries neither the key
nor the value. The three order tests pin which reason wins when a record holds
two defects at once.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Any

import pytest

from omnimarket.nodes.node_federation_egress_gate_compute.handlers.handler_federation_egress_gate import (
    HandlerFederationEgressGate,
    parse_value_refusing_duplicates,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.enum_egress_gate import (
    EnumEgressOutcome,
    EnumEgressRefusal,
)
from omnimarket.nodes.node_federation_egress_gate_compute.models.model_egress_gate import (
    ModelEgressField,
    ModelEgressGateRequest,
    ModelEgressGateResult,
    ModelEgressTopicPolicy,
    ModelHeaderRule,
    ModelWireHeader,
)

pytestmark = pytest.mark.unit

TOPIC = "example-tenant-a.commands"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
REASON_CODE_PATTERN = r"^[a-z-]{1,32}$"
MESSAGE_ID_PATTERN = r"^[0-9a-f-]{36}$"
COMMAND_ID = "0123456a-b2c3-4d5e-6f70-1234567890ab"
MESSAGE_ID = "1a2b3c4d-5e6f-0a1b-2c3d-3e4f5a6b7c8d"
OTHER_UUID = "ffffffff-1111-2222-3333-444455556666"

_WIDE_HEADER_RULES = (
    ModelHeaderRule(name="message-id", pattern=MESSAGE_ID_PATTERN),
    ModelHeaderRule(name="lane-id", pattern=r"^[a-z]{1,8}$"),
    ModelHeaderRule(name="trace-depth", pattern=r"^[0-9]{1,2}$"),
    ModelHeaderRule(name="span-id", pattern=r"^[0-9a-f]{1,8}$"),
)
_FOUR_HEADERS = (
    ("message-id", MESSAGE_ID),
    ("lane-id", "example"),
    ("trace-depth", "07"),
    ("span-id", "abc123"),
)
_FIVE_HEADERS = (*_FOUR_HEADERS, ("extra-id", "zz"))


def _policy(**overrides: Any) -> ModelEgressTopicPolicy:
    values: dict[str, Any] = {
        "fields": (
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
        "key_pattern": UUID_PATTERN,
        "key_binding": "command_id",
        "header_allow_list": (
            ModelHeaderRule(name="message-id", pattern=MESSAGE_ID_PATTERN),
        ),
        "max_headers": 4,
        "max_value_bytes": 4096,
        "max_value_depth": 8,
        "stamp_header_names": ("origin-lane", "source-lane", "hops"),
        "message_id_header": "message-id",
    }
    values.update(overrides)
    return ModelEgressTopicPolicy(**values)


def _request(
    value: bytes,
    *,
    key: str | None,
    headers: tuple[tuple[str, str], ...] = (("message-id", MESSAGE_ID),),
    policy: ModelEgressTopicPolicy | None = None,
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
        policy=policy if policy is not None else _policy(),
    )


def _member(name: str, literal: str) -> str:
    return f'"{name}":{literal}'


def _string(value: str) -> str:
    return f'"{value}"'


def _json_object(*members: str) -> bytes:
    return ("{" + ",".join(members) + "}").encode()


_GOOD = _json_object(_member("command_id", _string(COMMAND_ID)))


def _assert_leaks_nothing(
    result: ModelEgressGateResult,
    request: ModelEgressGateRequest,
    *,
    key: str | None,
) -> None:
    """No refusal answer carries the key, the value text or the base64."""
    dump = result.model_dump_json()
    if key:
        assert key not in dump
    assert request.value_b64 not in dump
    try:
        text = base64.b64decode(request.value_b64, validate=True).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return
    if text:
        assert text not in dump


# --------------------------------------------------------------------------
# shape-violation: one case per way a value can miss the closed shape.


def _shape_cases() -> list[tuple[str, ModelEgressGateRequest, ModelEgressGateRequest]]:
    command = _member("command_id", _string(COMMAND_ID))
    good = _request(_GOOD, key=COMMAND_ID)
    reason_ok = _request(
        _json_object(command, _member("reason_code", _string("ok"))),
        key=COMMAND_ID,
    )

    def with_attempt(literal: str) -> ModelEgressGateRequest:
        return _request(
            _json_object(command, _member("attempt", literal)), key=COMMAND_ID
        )

    deep = b'{"a":' * 9 + b"1" + b"}" * 9
    return [
        ("not-base64", good.model_copy(update={"value_b64": "not base64!"}), good),
        ("invalid-utf8", _request(b'{"command_id":"\xff\xfe"}', key=COMMAND_ID), good),
        ("utf8-bom", _request(b"\xef\xbb\xbf" + _GOOD, key=COMMAND_ID), good),
        ("top-level-array", _request(f"[{command}]".encode(), key=COMMAND_ID), good),
        ("nan", with_attempt("NaN"), with_attempt("3")),
        (
            "unknown-member",
            _request(
                _json_object(command, _member("surprise", _string("x"))),
                key=COMMAND_ID,
            ),
            good,
        ),
        (
            "missing-required-member",
            _request(b'{"reason_code":"ok"}', key=COMMAND_ID),
            good,
        ),
        ("wrong-type", _request(b'{"command_id":123}', key=COMMAND_ID), good),
        (
            "string-off-pattern",
            _request(
                _json_object(command, _member("reason_code", _string("NO-GOOD"))),
                key=COMMAND_ID,
            ),
            reason_ok,
        ),
        ("integer-out-of-bounds", with_attempt("10"), with_attempt("9")),
        ("bool-as-integer", with_attempt("true"), with_attempt("3")),
        ("nesting-past-max-depth", _request(deep, key=COMMAND_ID), good),
        (
            "unbalanced-brackets",
            _request(("{" + command).encode(), key=COMMAND_ID),
            good,
        ),
    ]


_SHAPE_CASE_LIST = _shape_cases()


@pytest.mark.parametrize(
    ("bad", "good"),
    [(bad, good) for _case_id, bad, good in _SHAPE_CASE_LIST],
    ids=[case_id for case_id, _bad, _good in _SHAPE_CASE_LIST],
)
def test_shape_violation_is_refused_and_the_control_passes(
    bad: ModelEgressGateRequest, good: ModelEgressGateRequest
) -> None:
    handler = HandlerFederationEgressGate()
    result = handler.handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.SHAPE_VIOLATION
    assert handler.handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=COMMAND_ID)


def test_nesting_past_max_value_depth_is_refused_at_the_parse() -> None:
    deep = b'{"a":' * 9 + b"1" + b"}" * 9
    policy = _policy()
    assert parse_value_refusing_duplicates(deep, max_depth=policy.max_value_depth) == (
        None,
        False,
    )
    parsed, has_duplicate = parse_value_refusing_duplicates(
        deep, max_depth=policy.max_value_depth + 1
    )
    assert parsed is not None
    assert has_duplicate is False


# --------------------------------------------------------------------------
# every other reason, one test each.


def test_oversize_is_refused_before_the_value_is_read() -> None:
    bad_value = _json_object(
        _member("command_id", _string(COMMAND_ID)),
        _member("reason_code", _string("x" * 4200)),
    )
    bad = _request(bad_value, key=COMMAND_ID)
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.OVERSIZE
    assert result.value_size == len(bad_value)
    good_value = _json_object(
        _member("command_id", _string(COMMAND_ID)),
        _member("reason_code", _string("x" * 32)),
    )
    good = _request(good_value, key=COMMAND_ID)
    assert HandlerFederationEgressGate().handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=COMMAND_ID)


def test_a_duplicate_member_is_refused_before_the_closed_model() -> None:
    raw = ('{"command_id":"not-a-uuid","command_id":"' + COMMAND_ID + '"}').encode()
    bad = _request(raw, key=COMMAND_ID)
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.DUPLICATE_MEMBER
    good = _request(_GOOD, key=COMMAND_ID)
    assert HandlerFederationEgressGate().handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=COMMAND_ID)


@pytest.mark.parametrize("key", [None, ""], ids=["no-key", "empty-key"])
def test_key_absent_is_refused_for_a_missing_and_an_empty_key(
    key: str | None,
) -> None:
    bad = _request(_GOOD, key=key)
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.KEY_ABSENT
    assert result.key_digest is None
    assert result.value_size == len(_GOOD)
    good = _request(_GOOD, key=COMMAND_ID)
    assert HandlerFederationEgressGate().handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=key)


@pytest.mark.parametrize(
    "key",
    ["kéy", "a\x00b", "not-a-uuid"],
    ids=["non-ascii", "nul-byte", "off-pattern"],
)
def test_key_shape_violation_is_refused(key: str) -> None:
    bad = _request(_GOOD, key=key)
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.KEY_SHAPE_VIOLATION
    assert result.key_digest == hashlib.sha256(key.encode("utf-8")).hexdigest()
    good = _request(_GOOD, key=COMMAND_ID)
    assert HandlerFederationEgressGate().handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=key)


def test_key_binding_mismatch_is_refused_for_a_well_shaped_key() -> None:
    bad = _request(_GOOD, key=OTHER_UUID)
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.KEY_BINDING_MISMATCH
    good = _request(_GOOD, key=COMMAND_ID)
    assert HandlerFederationEgressGate().handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=OTHER_UUID)


def test_a_duplicate_header_name_is_refused() -> None:
    bad = _request(
        _GOOD,
        key=COMMAND_ID,
        headers=(("message-id", MESSAGE_ID), ("message-id", MESSAGE_ID)),
    )
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.DUPLICATE_HEADER
    good = _request(_GOOD, key=COMMAND_ID)
    assert HandlerFederationEgressGate().handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=COMMAND_ID)


@pytest.mark.parametrize(
    "stamp_name",
    ["origin-lane", "source-lane", "hops", "ORIGIN-LANE"],
    ids=["origin-lane", "source-lane", "hops", "upper-case"],
)
def test_a_stamp_header_is_refused_in_every_spelling(stamp_name: str) -> None:
    bad = _request(
        _GOOD,
        key=COMMAND_ID,
        headers=(("message-id", MESSAGE_ID), (stamp_name, "example-lane-1")),
    )
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.STAMP_PRESENT
    good = _request(_GOOD, key=COMMAND_ID)
    assert HandlerFederationEgressGate().handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=COMMAND_ID)


def _header_cases() -> list[tuple[str, ModelEgressGateRequest, ModelEgressGateRequest]]:
    wide = _policy(header_allow_list=_WIDE_HEADER_RULES)
    return [
        (
            "unlisted-name",
            _request(
                _GOOD,
                key=COMMAND_ID,
                headers=(("message-id", MESSAGE_ID), ("x-trace-id", "abc123")),
            ),
            _request(_GOOD, key=COMMAND_ID),
        ),
        (
            "value-off-pattern",
            _request(
                _GOOD,
                key=COMMAND_ID,
                headers=(("message-id", "not-a-message-id"),),
            ),
            _request(_GOOD, key=COMMAND_ID),
        ),
        (
            "too-many-headers",
            _request(_GOOD, key=COMMAND_ID, headers=_FIVE_HEADERS, policy=wide),
            _request(_GOOD, key=COMMAND_ID, headers=_FOUR_HEADERS, policy=wide),
        ),
    ]


_HEADER_CASE_LIST = _header_cases()


@pytest.mark.parametrize(
    ("bad", "good"),
    [(bad, good) for _case_id, bad, good in _HEADER_CASE_LIST],
    ids=[case_id for case_id, _bad, _good in _HEADER_CASE_LIST],
)
def test_header_not_allowed_is_refused_and_the_control_passes(
    bad: ModelEgressGateRequest, good: ModelEgressGateRequest
) -> None:
    handler = HandlerFederationEgressGate()
    result = handler.handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.HEADER_NOT_ALLOWED
    assert handler.handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=COMMAND_ID)


def test_a_record_without_the_message_id_header_is_refused() -> None:
    bad = _request(_GOOD, key=COMMAND_ID, headers=())
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.NO_MESSAGE_ID
    good = _request(_GOOD, key=COMMAND_ID)
    assert HandlerFederationEgressGate().handle(good).outcome is EnumEgressOutcome.PASS
    _assert_leaks_nothing(result, bad, key=COMMAND_ID)


# --------------------------------------------------------------------------
# which reason wins when a record holds two defects at once.


def test_oversize_is_reported_before_a_duplicate_member() -> None:
    raw = _json_object(
        _member("command_id", _string("not-a-uuid")),
        _member("command_id", _string(COMMAND_ID)),
        _member("reason_code", _string("y" * 4200)),
    )
    bad = _request(raw, key=COMMAND_ID)
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.OVERSIZE
    _assert_leaks_nothing(result, bad, key=COMMAND_ID)


def test_a_duplicate_member_is_reported_before_a_bad_key() -> None:
    raw = ('{"command_id":"not-a-uuid","command_id":"' + COMMAND_ID + '"}').encode()
    bad = _request(raw, key="not-a-uuid")
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.DUPLICATE_MEMBER
    _assert_leaks_nothing(result, bad, key="not-a-uuid")


def test_a_bad_key_is_reported_before_a_stamp_header() -> None:
    bad = _request(
        _GOOD,
        key="not-a-uuid",
        headers=(("message-id", MESSAGE_ID), ("origin-lane", "example-lane-1")),
    )
    result = HandlerFederationEgressGate().handle(bad)
    assert result.outcome is EnumEgressOutcome.REFUSE
    assert result.reason is EnumEgressRefusal.KEY_SHAPE_VIOLATION
    _assert_leaks_nothing(result, bad, key="not-a-uuid")
