# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed refusal vocabularies of the federation edge decision compute.

A refusal name is the word a refused edge step or overlay entry carries. The
last ten values of ``EnumEdgeRefusal`` are the egress gate's refusal
names, word for word, so an edge refusal and the gate refusal that caused it
carry the same name. A value that is not listed is refused at the model
boundary, so a new reason can only arrive by changing this file and the tests
that pin it.
"""

from __future__ import annotations

from enum import StrEnum


class EnumEdgeRefusal(StrEnum):
    """Why one edge step is refused (a pass carries no reason)."""

    UNIT_NOT_SINGLE = "unit-not-single"
    TOPIC_NOT_ALLOWED = "topic-not-allowed"
    CLASS_NOT_DECLARED = "class-not-declared"
    LANE_UNAUTHENTICATED = "lane-unauthenticated"
    PRINCIPAL_BINDING_MISMATCH = "principal-binding-mismatch"
    LANE_NOT_PERMITTED = "lane-not-permitted"
    ROUTE_OUTSIDE_TENANT = "route-outside-tenant"
    NOW_ABSENT = "now-absent"
    ACL_NOT_MEASURED = "acl-not-measured"
    ACL_PROBE_MISMATCH = "acl-probe-mismatch"
    ACL_PROBE_STALE = "acl-probe-stale"
    ACL_PROBE_NOT_DENIED = "acl-probe-not-denied"
    OVERSIZE = "oversize"
    DUPLICATE_MEMBER = "duplicate-member"
    SHAPE_VIOLATION = "shape-violation"
    KEY_ABSENT = "key-absent"
    KEY_SHAPE_VIOLATION = "key-shape-violation"
    KEY_BINDING_MISMATCH = "key-binding-mismatch"
    HEADER_NOT_ALLOWED = "header-not-allowed"
    DUPLICATE_HEADER = "duplicate-header"
    NO_MESSAGE_ID = "no-message-id"
    STAMP_PRESENT = "stamp-present"


class EnumOverlayRefusal(StrEnum):
    """Why an overlay file's tables refuse to close (its validation result)."""

    BINDING_NOT_IN_TABLE = "binding-not-in-table"
    PRINCIPAL_SHARED = "principal-shared"
    LANE_UNKNOWN = "lane-unknown"
    LANE_UNAUTHENTICATED = "lane-unauthenticated"
    LANE_NOT_PERMITTED = "lane-not-permitted"
    PRINCIPAL_NOT_GRANTED_ON_LANE = "principal-not-granted-on-lane"
    TOPIC_NOT_REGISTERED = "topic-not-registered"
    TOPIC_CLASS_NOT_INTERNAL = "topic-class-not-internal"
    TOPIC_ATOMIC_GROUP = "topic-atomic-group"
    KEY_BINDING_FIELD_MISSING = "key-binding-field-missing"
    MODEL_NOT_CLOSED = "model-not-closed"


__all__: list[str] = [
    "EnumEdgeRefusal",
    "EnumOverlayRefusal",
]
