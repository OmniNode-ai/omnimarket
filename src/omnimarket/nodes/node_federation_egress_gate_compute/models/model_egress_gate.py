# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the federation egress gate compute.

The policy (``ModelEgressTopicPolicy``) arrives inside the request, so this
node has no overlay of its own: every limit, name and pattern is the topic's,
not the node's. The request carries the record as base64 (value, optional
key) plus the wire headers; the result says pass or refuse with one named
reason and never carries the key or the value — only a sha256 hex digest of
the key bytes and the value size.

Every pattern a policy may declare is checked at the model boundary by
``assert_bounded_anchored_pattern``: anchored, bounded and short, so no
pattern held here can scan unboundedly.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_federation_egress_gate_compute.models.enum_egress_gate import (
    EnumEgressOutcome,
    EnumEgressRefusal,
)

# The longest pattern a policy may declare. A bound on the policy's own text,
# not a policy setting: patterns arrive in the request and are refused here.
PATTERN_MAX_LENGTH = 512

# A counted repeat, as ``re`` reads one: ``{n}``, ``{n,}``, ``{n,m}``, ``{,m}``. Anything else after a ``{`` is a literal brace.
_COUNTED_REPEAT = re.compile(r"\{(\d*)(?:(,)(\d*))?\}")


def _ends_with_unescaped_dollar(pattern: str) -> bool:
    """Whether the pattern ends in a ``$`` no preceding backslash run escapes."""
    if not pattern.endswith("$"):
        return False
    backslashes = 0
    for char in reversed(pattern[:-1]):
        if char != "\\":
            break
        backslashes += 1
    return backslashes % 2 == 0


def _holds_unbounded_repeat(pattern: str) -> bool:
    """Whether the pattern holds a repeat with no upper bound anywhere in it.

    A lexical scan over the public pattern text: ``*`` and ``+`` outside a character class and not escaped (so ``*?``, ``+?``
    and ``++`` are caught with them), and a counted repeat with a comma and no upper number (``{n,}`` and ``{,}``). A repeat
    inside a group, an alternation, a lookaround or another bounded repeat is found the same way, because the scan does not
    care what the repeat sits in.
    """
    index = 0
    in_class = False
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            index += 1
            continue
        if char == "[":
            in_class = True
            index += 1
            # a ``]`` straight after ``[`` or ``[^`` is a literal member of the class
            if pattern.startswith("^", index):
                index += 1
            if pattern.startswith("]", index):
                index += 1
            continue
        if char in "*+":
            return True
        if char == "{":
            counted = _COUNTED_REPEAT.match(pattern, index)
            if counted is not None and counted.group(2) and not counted.group(3):
                return True
        index += 1
    return False


def assert_bounded_anchored_pattern(pattern: str, *, what: str) -> str:
    """Return ``pattern``, or raise ``ValueError`` naming ``what`` when the
    pattern does not compile, is not anchored (a leading ``^`` and a trailing,
    unescaped ``$``), holds an unbounded repeat anywhere (``*``, ``+``,
    ``{n,}``), or is longer than ``PATTERN_MAX_LENGTH`` characters.
    """
    if len(pattern) > PATTERN_MAX_LENGTH:
        raise ValueError(f"{what} is longer than {PATTERN_MAX_LENGTH} characters")
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError(f"{what} does not compile: {error}") from error
    if not pattern.startswith("^") or not _ends_with_unescaped_dollar(pattern):
        raise ValueError(f"{what} must start with ^ and end with an unescaped $")
    if _holds_unbounded_repeat(pattern):
        raise ValueError(f"{what} must not hold an unbounded repeat")
    return pattern


class ModelEgressField(BaseModel):
    """One member the topic's value shape allows, with the kind's own bounds.

    A ``string`` field carries an anchored bounded ``pattern`` and a
    ``max_length`` (1..4096); an ``integer`` field carries ``minimum`` and
    ``maximum``; a ``boolean`` field carries none of the four.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    kind: Literal["string", "integer", "boolean"]
    required: bool
    pattern: str | None = None
    max_length: int | None = Field(default=None, ge=1, le=4096)
    minimum: int | None = None
    maximum: int | None = None

    @model_validator(mode="after")
    def _kind_carries_its_own_bounds(self) -> ModelEgressField:
        if self.kind == "string":
            if self.pattern is None:
                raise ValueError(f"field {self.name!r}: kind string requires a pattern")
            if self.max_length is None:
                raise ValueError(
                    f"field {self.name!r}: kind string requires a max_length"
                )
            if self.minimum is not None or self.maximum is not None:
                raise ValueError(
                    f"field {self.name!r}: kind string forbids minimum/maximum"
                )
            assert_bounded_anchored_pattern(
                self.pattern,
                what=f"ModelEgressField.pattern of field {self.name!r}",
            )
        elif self.kind == "integer":
            if self.pattern is not None or self.max_length is not None:
                raise ValueError(
                    f"field {self.name!r}: kind integer forbids pattern/max_length"
                )
            if self.minimum is None or self.maximum is None:
                raise ValueError(
                    f"field {self.name!r}: kind integer requires minimum and maximum"
                )
            if self.minimum > self.maximum:
                raise ValueError(
                    f"field {self.name!r}: kind integer minimum exceeds maximum"
                )
        elif (
            self.pattern is not None
            or self.max_length is not None
            or self.minimum is not None
            or self.maximum is not None
        ):
            raise ValueError(
                f"field {self.name!r}: kind boolean forbids pattern, max_length, "
                "minimum and maximum"
            )
        return self


class ModelHeaderRule(BaseModel):
    """One wire-header name the topic allows, with the value's bounded pattern."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    pattern: str

    @model_validator(mode="after")
    def _pattern_is_bounded(self) -> ModelHeaderRule:
        assert_bounded_anchored_pattern(
            self.pattern, what=f"ModelHeaderRule.pattern of header {self.name!r}"
        )
        return self


class ModelWireHeader(BaseModel):
    """One header as it arrived on the wire: a name and its raw value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1)
    value: str


class ModelEgressTopicPolicy(BaseModel):
    """The one topic's egress policy, arriving inside the request.

    The value shape is closed: a member not named in ``fields`` refuses the
    record. ``key_binding`` names the string field whose value the key must
    equal. ``header_allow_list`` names the only headers allowed, with each
    value's pattern; ``stamp_header_names`` are this bus's own headers, never
    allowed out; ``message_id_header`` names the allow-list rule the record
    must carry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fields: tuple[ModelEgressField, ...] = Field(..., min_length=1)
    key_pattern: str
    key_binding: str
    header_allow_list: tuple[ModelHeaderRule, ...]
    max_headers: int = Field(..., ge=1)
    max_value_bytes: int = Field(..., ge=1)
    max_value_depth: int = Field(..., ge=1)
    stamp_header_names: tuple[str, ...] = Field(..., min_length=1)
    message_id_header: str

    @field_validator("key_pattern")
    @classmethod
    def _key_pattern_is_bounded(cls, value: str) -> str:
        return assert_bounded_anchored_pattern(
            value, what="ModelEgressTopicPolicy.key_pattern"
        )

    @model_validator(mode="after")
    def _bindings_name_closed_members(self) -> ModelEgressTopicPolicy:
        fields_by_name = {entry.name: entry for entry in self.fields}
        if len(fields_by_name) != len(self.fields):
            raise ValueError("field names are not unique")
        bound = fields_by_name.get(self.key_binding)
        if bound is None or bound.kind != "string":
            raise ValueError(
                f"key_binding {self.key_binding!r} must name a field of kind string"
            )
        rules_by_name = {rule.name: rule for rule in self.header_allow_list}
        if len(rules_by_name) != len(self.header_allow_list):
            raise ValueError("header allow-list names are not unique")
        if self.message_id_header not in rules_by_name:
            raise ValueError(
                f"message_id_header {self.message_id_header!r} must name a "
                "header_allow_list rule"
            )
        stamp_lower = {name.lower() for name in self.stamp_header_names}
        for rule in self.header_allow_list:
            if rule.name.lower() in stamp_lower:
                raise ValueError(
                    f"header allow-list name {rule.name!r} equals a stamp header name"
                )
        return self


class ModelEgressGateRequest(BaseModel):
    """One record leaving one bus for another, as base64, with its headers.

    ``value_b64`` is the record's bytes, ``key_b64`` the optional key bytes;
    neither is decoded here, only bounded by the model. ``policy`` is the
    topic's egress policy, so the node reads no overlay of its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., min_length=1)
    key_b64: str | None = None
    value_b64: str
    headers: tuple[ModelWireHeader, ...] = ()
    policy: ModelEgressTopicPolicy


class ModelEgressGateResult(BaseModel):
    """The gate's answer for one record.

    ``key_digest`` is the sha256 hex of the key bytes and ``None`` when no key
    was carried; ``value_size`` the decoded value's byte length; ``message_id``
    the message-id header's value on a pass. The key and the value themselves
    are never carried.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumEgressOutcome
    reason: EnumEgressRefusal | None = None
    key_digest: str | None = None
    value_size: int = Field(..., ge=0)
    message_id: str | None = None

    @model_validator(mode="after")
    def _reason_agrees_with_outcome(self) -> ModelEgressGateResult:
        if self.outcome is EnumEgressOutcome.PASS and self.reason is not None:
            raise ValueError("a pass result carries no refusal reason")
        if self.outcome is EnumEgressOutcome.REFUSE and self.reason is None:
            raise ValueError("a refuse result names one refusal reason")
        return self


__all__: list[str] = [
    "PATTERN_MAX_LENGTH",
    "EnumEgressOutcome",
    "EnumEgressRefusal",
    "ModelEgressField",
    "ModelEgressGateRequest",
    "ModelEgressGateResult",
    "ModelEgressTopicPolicy",
    "ModelHeaderRule",
    "ModelWireHeader",
    "assert_bounded_anchored_pattern",
]
