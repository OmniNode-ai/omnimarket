# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closed set of causes a seam side records beside a blocked, refused or stale id.

Core's empty-state reasons that apply keep their value (`upstream-blocked`, `schema-invalid`);
the rest are this seam family's.
"""

from __future__ import annotations

from enum import StrEnum


class EnumSeamCause(StrEnum):
    UPSTREAM_BLOCKED = "upstream-blocked"
    SCHEMA_INVALID = "schema-invalid"
    DUPLICATE_KEY = "duplicate-key"
    NO_MATCHING_PLAN = "no-matching-plan"
    COMPACTED = "compacted"
    OUT_OF_BAND = "out-of-band"
    MISSING_VERDICT = "missing-verdict"
    SUMMARISED = "summarised"
    TRUNCATED = "truncated"
    UNUSED = "unused"
    MISSING = "missing"
    UNMAPPED = "unmapped"
    DIGEST_MISMATCH = "digest-mismatch"
    ORPHANED = "orphaned"
    TRANSFER_FAILURE = "transfer-failure"
    CHANNEL_NOT_FOUND = "channel-not-found"
    RATE_LIMITED = "rate-limited"
    AUTH = "auth"
    VERIFY_WIDGET_ENVELOPE = "verify-widget-envelope-failed"
    LATENCY_INCOMPLETE = "latency-incomplete"
    LATE = "late"
