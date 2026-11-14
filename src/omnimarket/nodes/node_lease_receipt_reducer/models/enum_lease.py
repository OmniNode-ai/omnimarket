# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed vocabularies of the lease receipt reducer.

Every value here is a word the lease reducer's rules use. A value that is not
listed is refused at the model boundary, so a new operation, release rule,
status or refusal reason can only arrive by changing this file and the tests
that pin it. The refusal reasons are the typed codes a refused reply carries;
``none`` is the reason of a recorded reply.
"""

from __future__ import annotations

from enum import StrEnum


class EnumLeaseOperation(StrEnum):
    """The operations one lease-reduce command may ask for."""

    ACQUIRE = "acquire"
    RENEW = "renew"
    RELEASE = "release"
    REVOKE = "revoke"
    TAKE_OVER = "take_over"
    RECORD_RECEIPT = "record_receipt"


class EnumLeaseReleaseRule(StrEnum):
    """What lets a kind's lease be released, and only then taken over while live."""

    EVIDENCE = "evidence"
    EXPIRY = "expiry"


class EnumLeaseStatus(StrEnum):
    """The two outcomes of a reduce: recorded, or refused with a reason."""

    RECORDED = "recorded"
    REFUSED = "refused"


class EnumLeaseReason(StrEnum):
    """The typed refusal codes, and ``none`` for a recorded result."""

    NONE = "none"
    CALLER_NOT_ADMITTED = "caller-not-admitted"
    UNKNOWN_KIND = "unknown-kind"
    OPERATION_NOT_ENABLED = "operation-not-enabled-for-kind"
    SUBJECT_PATTERN = "subject-pattern-mismatch"
    TTL_EXCEEDED = "ttl-exceeded"
    MISSING_FIELD = "missing-field"
    HELD = "held"
    NOT_HELD = "not-held"
    EPOCH_MISMATCH = "epoch-mismatch"
    HOLDER_MISMATCH = "holder-mismatch"
    EPOCH_BEATEN = "epoch-beaten"
    NOT_TAKEABLE = "not-takeable"
    EVIDENCE_REQUIRED = "evidence-required"
    NOT_EXPIRED = "not-expired"
    STALE_EPOCH = "stale-epoch"
    REVOKED_EPOCH = "revoked-epoch"
    UNKNOWN_EPOCH = "unknown-epoch"
    LEASE_RELEASED = "lease-released"
    ALREADY_RECORDED = "already-recorded"
    WRONG_SUBJECT = "wrong-subject"
    HOLDER_RUN_MISMATCH = "holder-run-mismatch"
    OUTCOME_REF_INVALID = "outcome-ref-invalid"


__all__: list[str] = [
    "EnumLeaseOperation",
    "EnumLeaseReason",
    "EnumLeaseReleaseRule",
    "EnumLeaseStatus",
]
