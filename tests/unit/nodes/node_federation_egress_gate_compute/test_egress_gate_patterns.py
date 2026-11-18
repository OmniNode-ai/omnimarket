# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pattern boundary: every declared pattern is anchored, bounded and short.

``assert_bounded_anchored_pattern`` is the one check every pattern a policy may
declare passes through; the models refuse a field, a header rule or a policy
that misses its own kind's bounds or names a member that is not there.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.federation_egress.model_egress_gate import (
    ModelEgressField,
    ModelEgressTopicPolicy,
    ModelHeaderRule,
    assert_bounded_anchored_pattern,
)

pytestmark = pytest.mark.unit

UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
REASON_CODE_PATTERN = r"^[a-z-]{1,32}$"
MESSAGE_ID_PATTERN = r"^[0-9a-f-]{36}$"

COMMAND_ID_FIELD = ModelEgressField(
    name="command_id",
    kind="string",
    required=True,
    pattern=UUID_PATTERN,
    max_length=36,
)
REASON_CODE_FIELD = ModelEgressField(
    name="reason_code",
    kind="string",
    required=False,
    pattern=REASON_CODE_PATTERN,
    max_length=32,
)
ATTEMPT_FIELD = ModelEgressField(
    name="attempt", kind="integer", required=False, minimum=0, maximum=9
)


def _policy(**overrides: Any) -> ModelEgressTopicPolicy:
    values: dict[str, Any] = {
        "fields": (COMMAND_ID_FIELD, REASON_CODE_FIELD, ATTEMPT_FIELD),
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


# --------------------------------------------------------------------------
# assert_bounded_anchored_pattern


@pytest.mark.parametrize(
    "pattern",
    [
        "^a*$",
        "^a+$",
        "^a{2,}$",
        "^(ab+)$",
        r"^[a-z]{1,}$",
        "a$",
        "^a",
        "^" + "a" * 512 + "$",
        "^(a$",
    ],
    ids=[
        "star",
        "plus",
        "open-brace",
        "grouped-plus",
        "class-open-brace",
        "unanchored-start",
        "unanchored-end",
        "too-long",
        "does-not-compile",
    ],
)
def test_refuses_unbounded_unanchored_or_overlong_patterns(pattern: str) -> None:
    with pytest.raises(ValueError, match="test pattern"):
        assert_bounded_anchored_pattern(pattern, what="test pattern")


@pytest.mark.parametrize(
    "pattern", [UUID_PATTERN, "^[a-z]{1,8}$"], ids=["uuid", "bounded-class"]
)
def test_accepts_bounded_anchored_patterns(pattern: str) -> None:
    assert assert_bounded_anchored_pattern(pattern, what="test pattern") == pattern


# --------------------------------------------------------------------------
# ModelEgressField


def test_a_string_field_requires_a_pattern() -> None:
    with pytest.raises(ValidationError):
        ModelEgressField(name="example", kind="string", required=True, max_length=8)


def test_an_integer_field_requires_bounds() -> None:
    with pytest.raises(ValidationError):
        ModelEgressField(name="example", kind="integer", required=True)


def test_a_boolean_field_forbids_a_pattern() -> None:
    with pytest.raises(ValidationError):
        ModelEgressField(name="example", kind="boolean", required=True, pattern="^x$")


# --------------------------------------------------------------------------
# ModelEgressTopicPolicy


def test_key_binding_must_name_a_string_field() -> None:
    with pytest.raises(ValidationError):
        _policy(key_binding="attempt")


def test_key_binding_must_name_a_present_field() -> None:
    with pytest.raises(ValidationError):
        _policy(key_binding="ghost")


def test_message_id_header_must_name_an_allow_list_rule() -> None:
    with pytest.raises(ValidationError):
        _policy(message_id_header="trace-id")


def test_an_allow_list_name_may_not_equal_a_stamp_name() -> None:
    with pytest.raises(ValidationError):
        _policy(
            header_allow_list=(
                ModelHeaderRule(name="Origin-Lane", pattern=r"^[a-z]{1,8}$"),
            ),
            message_id_header="Origin-Lane",
        )


def test_field_names_must_be_unique() -> None:
    with pytest.raises(ValidationError):
        _policy(
            fields=(COMMAND_ID_FIELD, COMMAND_ID_FIELD, ATTEMPT_FIELD),
        )
