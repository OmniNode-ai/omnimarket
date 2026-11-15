# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFederationEgressGate: the pure gate for one record leaving one bus.

A record crossing between buses is checked for SHAPE and the key is BOUND to
the value it names; nothing is transformed. ``handle(request:
ModelEgressGateRequest) -> ModelEgressGateResult`` is sync and pure: no I/O,
no clock, no envelope, and it returns or logs neither the key nor the value —
only a sha256 hex digest of the key bytes and the value size.

The checks run in this order and the first failure returns the refusal, with
``value_size`` and ``key_digest`` filled when they are computable and
``message_id`` only once the header was read:

0. Base64 decode of the value and, when carried, the key
   (``shape-violation``).
1. The decoded value's size against the policy's ``max_value_bytes``
   (``oversize``).
2. The parse (``parse_value_refusing_duplicates``): duplicate members are
   reported before any shape violation, and a non-object or unparseable text
   is a ``shape-violation``.
3. The closed model: every member a named field, every required field
   present, every value of its field's kind and bounds
   (``shape-violation``).
4. The key: present (``key-absent``), strict ASCII with no control character
   and a full match of the policy's ``key_pattern``
   (``key-shape-violation``), then equal to the value of the policy's bound
   field (``key-binding-mismatch``).
5. The headers: no name twice (``duplicate-header``), no stamp header named
   (``stamp-present``), the count, the allow-list rule and the value pattern
   per name (``header-not-allowed``), and the message-id header the policy
   names present (``no-message-id``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re

from omnimarket.models.federation_egress.enum_egress_gate import (
    EnumEgressOutcome,
    EnumEgressRefusal,
)
from omnimarket.models.federation_egress.model_egress_gate import (
    ModelEgressField,
    ModelEgressGateRequest,
    ModelEgressGateResult,
    ModelEgressTopicPolicy,
)

# A UTF-8 BOM is never a JSON text, not even an empty object's.
_UTF8_BOM = b"\xef\xbb\xbf"

# The control characters a key may not hold: 0x00-0x1f and 0x7f.
_KEY_CONTROL_MAX = 0x1F
_KEY_DEL = 0x7F


def _reject_constant(name: str) -> float:
    """Refuse the JSON constants ``NaN``, ``Infinity`` and ``-Infinity``."""
    raise ValueError(f"{name} is not a value this gate parses")


def _depth_prescan_balanced(text: str, *, max_depth: int) -> bool:
    """Whether the text's ``{``/``[`` nesting stays within ``max_depth`` and
    every bracket closes its own opener, reading through strings and their
    backslash escapes. Iterative: the scan itself recurses on nothing.
    """
    openers: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            openers.append(char)
            if len(openers) > max_depth:
                return False
        elif char in "}]":
            if not openers or openers[-1] != ("{" if char == "}" else "["):
                return False
            openers.pop()
    return not in_string and not escaped and not openers


def parse_value_refusing_duplicates(
    value: bytes, *, max_depth: int
) -> tuple[dict[str, object] | None, bool]:
    """Parse one record's value, refusing duplicate members, not last-naming.

    Returns the parsed object — ``None`` when the bytes are not exactly one
    valid JSON object — and whether any object at any depth, inside arrays
    included, held two pairs of the same decoded member name (case-sensitive,
    after the decoder's escape decoding). A UTF-8 BOM, invalid UTF-8, nesting
    past ``max_depth``, unbalanced brackets, a JSON error or a non-object top
    level are not parseable: ``(None, False)``. The duplicate record counts
    only when the whole text parses, so a duplicate inside a broken text is
    reported as a shape violation by the caller.
    """
    if value.startswith(_UTF8_BOM):
        return None, False
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None, False
    if not _depth_prescan_balanced(text, max_depth=max_depth):
        return None, False
    has_duplicate = False

    def _record_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal has_duplicate
        seen: set[str] = set()
        for name, _item in pairs:
            if name in seen:
                has_duplicate = True
            seen.add(name)
        return dict(pairs)

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_record_duplicates,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError):
        return None, False
    if not isinstance(parsed, dict):
        return None, False
    return parsed, has_duplicate


def _field_value_violation(field: ModelEgressField, value: object) -> bool:
    """Whether one present member's value misses its field's kind and bounds."""
    if field.kind == "string":
        if not isinstance(value, str):
            return True
        max_length = field.max_length
        if max_length is None or len(value) > max_length:
            return True
        pattern = field.pattern
        return pattern is None or re.fullmatch(pattern, value) is None
    if field.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return True
        minimum = field.minimum
        maximum = field.maximum
        return minimum is None or maximum is None or value < minimum or value > maximum
    return not isinstance(value, bool)


def _closed_model_violation(
    policy: ModelEgressTopicPolicy, parsed: dict[str, object]
) -> bool:
    """Whether the parsed object misses the policy's closed value shape: an
    unknown member, a missing required field, or a value off its field."""
    fields_by_name = {field.name: field for field in policy.fields}
    for name in parsed:
        if name not in fields_by_name:
            return True
    for field in policy.fields:
        if field.required and field.name not in parsed:
            return True
    return any(
        _field_value_violation(field, parsed[field.name])
        for field in policy.fields
        if field.name in parsed
    )


def _key_digest_of(key_bytes: bytes) -> str:
    """The only form of the key the gate ever holds or returns: its digest."""
    return hashlib.sha256(key_bytes).hexdigest()


def _refuse(
    reason: EnumEgressRefusal,
    *,
    key_digest: str | None,
    value_size: int,
) -> ModelEgressGateResult:
    """One refusal: the named reason, the key only as its digest, the size."""
    return ModelEgressGateResult(
        outcome=EnumEgressOutcome.REFUSE,
        reason=reason,
        key_digest=key_digest,
        value_size=value_size,
        message_id=None,
        terminal_failure_cause=reason,
    )


class HandlerFederationEgressGate:
    """The federation egress gate: pure compute, one record in, one answer out."""

    def handle(self, request: ModelEgressGateRequest) -> ModelEgressGateResult:
        """Gate one record against the topic policy in the request; the same
        request gives the same answer, and neither the key nor the value (nor
        any form of them but the digest and the size) leaves this function."""
        policy = request.policy

        # 0. Decode the value and, when carried, the key. A failed decode of
        # either is a shape violation; the size and the digest are filled with
        # whatever the other side still computed.
        value_bytes: bytes | None
        try:
            value_bytes = base64.b64decode(request.value_b64, validate=True)
        except (binascii.Error, ValueError):
            value_bytes = None
        key_bytes: bytes | None = None
        key_digest: str | None = None
        key_failed = False
        if request.key_b64 is not None:
            try:
                key_bytes = base64.b64decode(request.key_b64, validate=True)
                key_digest = _key_digest_of(key_bytes)
            except (binascii.Error, ValueError):
                key_failed = True
        if value_bytes is None or key_failed:
            return _refuse(
                EnumEgressRefusal.SHAPE_VIOLATION,
                key_digest=key_digest,
                value_size=0 if value_bytes is None else len(value_bytes),
            )
        value_size = len(value_bytes)

        # 1. The value's size, before anything reads it.
        if value_size > policy.max_value_bytes:
            return _refuse(
                EnumEgressRefusal.OVERSIZE,
                key_digest=key_digest,
                value_size=value_size,
            )

        # 2. The parse. A duplicate member is reported before any shape
        # violation, including before the not-an-object report.
        parsed, has_duplicate = parse_value_refusing_duplicates(
            value_bytes, max_depth=policy.max_value_depth
        )
        if has_duplicate:
            return _refuse(
                EnumEgressRefusal.DUPLICATE_MEMBER,
                key_digest=key_digest,
                value_size=value_size,
            )
        if parsed is None:
            return _refuse(
                EnumEgressRefusal.SHAPE_VIOLATION,
                key_digest=key_digest,
                value_size=value_size,
            )

        # 3. The closed model: nothing unnamed, nothing required missing,
        # every present value of its field's kind and bounds.
        if _closed_model_violation(policy, parsed):
            return _refuse(
                EnumEgressRefusal.SHAPE_VIOLATION,
                key_digest=key_digest,
                value_size=value_size,
            )

        # 4. The key: present, shaped, and bound to the value it names. An
        # empty key is no key.
        if key_bytes is None or not key_bytes:
            return _refuse(
                EnumEgressRefusal.KEY_ABSENT, key_digest=None, value_size=value_size
            )
        try:
            key_text = key_bytes.decode("ascii")
        except UnicodeDecodeError:
            return _refuse(
                EnumEgressRefusal.KEY_SHAPE_VIOLATION,
                key_digest=key_digest,
                value_size=value_size,
            )
        if any(
            ord(char) <= _KEY_CONTROL_MAX or ord(char) == _KEY_DEL for char in key_text
        ):
            return _refuse(
                EnumEgressRefusal.KEY_SHAPE_VIOLATION,
                key_digest=key_digest,
                value_size=value_size,
            )
        if re.fullmatch(policy.key_pattern, key_text) is None:
            return _refuse(
                EnumEgressRefusal.KEY_SHAPE_VIOLATION,
                key_digest=key_digest,
                value_size=value_size,
            )
        bound_value = parsed.get(policy.key_binding)
        if not isinstance(bound_value, str) or bound_value != key_text:
            return _refuse(
                EnumEgressRefusal.KEY_BINDING_MISMATCH,
                key_digest=key_digest,
                value_size=value_size,
            )

        # 5. The headers: no name twice, no stamp header named, then the
        # count, the allow-list rule and the value pattern per name, then the
        # message-id header the policy names.
        seen_names: set[str] = set()
        for header in request.headers:
            if header.name in seen_names:
                return _refuse(
                    EnumEgressRefusal.DUPLICATE_HEADER,
                    key_digest=key_digest,
                    value_size=value_size,
                )
            seen_names.add(header.name)
        stamp_names_lower = {name.lower() for name in policy.stamp_header_names}
        for header in request.headers:
            if header.name.lower() in stamp_names_lower:
                return _refuse(
                    EnumEgressRefusal.STAMP_PRESENT,
                    key_digest=key_digest,
                    value_size=value_size,
                )
        rules_by_name = {rule.name: rule for rule in policy.header_allow_list}
        if len(request.headers) > policy.max_headers:
            return _refuse(
                EnumEgressRefusal.HEADER_NOT_ALLOWED,
                key_digest=key_digest,
                value_size=value_size,
            )
        for header in request.headers:
            rule = rules_by_name.get(header.name)
            if rule is None or re.fullmatch(rule.pattern, header.value) is None:
                return _refuse(
                    EnumEgressRefusal.HEADER_NOT_ALLOWED,
                    key_digest=key_digest,
                    value_size=value_size,
                )
        message_id: str | None = None
        for header in request.headers:
            if header.name == policy.message_id_header:
                message_id = header.value
                break
        if message_id is None:
            return _refuse(
                EnumEgressRefusal.NO_MESSAGE_ID,
                key_digest=key_digest,
                value_size=value_size,
            )
        return ModelEgressGateResult(
            outcome=EnumEgressOutcome.PASS,
            reason=None,
            key_digest=key_digest,
            value_size=value_size,
            message_id=message_id,
        )


def gate_egress(request: ModelEgressGateRequest) -> ModelEgressGateResult:
    """One gate decision: the handler's pure answer for one record."""
    return HandlerFederationEgressGate().handle(request)


__all__: list[str] = [
    "HandlerFederationEgressGate",
    "gate_egress",
    "parse_value_refusing_duplicates",
]
