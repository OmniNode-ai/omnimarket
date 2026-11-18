# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed vocabularies of the federation egress gate.

The gate answers pass or refuse; a refusal carries exactly one named reason
from ``EnumEgressRefusal``. A value that is not listed is refused at the model
boundary, so a new reason can only arrive by changing this file.
"""

from __future__ import annotations

from enum import StrEnum


class EnumEgressOutcome(StrEnum):
    """What the gate decided about one record leaving one bus for another."""

    PASS = "pass"
    REFUSE = "refuse"


class EnumEgressRefusal(StrEnum):
    """Why the gate refused one record, one word per check, in gate order.

    ``oversize``: the value is longer than the policy's ``max_value_bytes``.
    ``duplicate-member``: two pairs of one object carry the same member name,
    at any depth. ``shape-violation``: the value is not one decodable, parseable
    JSON object matching the closed model, or the key or a header field failed
    to decode. ``key-absent``: no key was carried. ``key-shape-violation``: the
    key is not the policy's ``key_pattern``. ``key-binding-mismatch``: the key
    does not equal the value of the bound field. ``stamp-present``: a header
    named a stamp header. ``duplicate-header``: one header name twice.
    ``header-not-allowed``: too many headers, a name outside the allow list or
    a value off its rule's pattern. ``no-message-id``: the message-id header
    the policy names is missing.
    """

    OVERSIZE = "oversize"
    DUPLICATE_MEMBER = "duplicate-member"
    SHAPE_VIOLATION = "shape-violation"
    KEY_ABSENT = "key-absent"
    KEY_SHAPE_VIOLATION = "key-shape-violation"
    KEY_BINDING_MISMATCH = "key-binding-mismatch"
    STAMP_PRESENT = "stamp-present"
    DUPLICATE_HEADER = "duplicate-header"
    HEADER_NOT_ALLOWED = "header-not-allowed"
    NO_MESSAGE_ID = "no-message-id"


__all__: list[str] = [
    "EnumEgressOutcome",
    "EnumEgressRefusal",
]
