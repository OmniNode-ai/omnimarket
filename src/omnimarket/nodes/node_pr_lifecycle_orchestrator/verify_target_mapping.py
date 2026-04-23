# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Changed-file-to-verification-target mapping for PR lifecycle verify phase (OMN-7742)."""

from __future__ import annotations

import re
from enum import StrEnum


class EnumVerificationTarget(StrEnum):
    PROJECTION_ROW_CHECK = "PROJECTION_ROW_CHECK"
    PROJECTION_SINK_CHECK = "PROJECTION_SINK_CHECK"
    API_ROUTE_CHECK = "API_ROUTE_CHECK"
    DB_MIGRATION_CHECK = "DB_MIGRATION_CHECK"
    KAFKA_TOPIC_CHECK = "KAFKA_TOPIC_CHECK"
    SKIPPED_NO_MAPPING = "SKIPPED_NO_MAPPING"


class EnumVerificationOutcome(StrEnum):
    MERGED = "MERGED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    VERIFICATION_UNAVAILABLE = "VERIFICATION_UNAVAILABLE"
    VERIFICATION_TIMEOUT = "VERIFICATION_TIMEOUT"
    VERIFICATION_TOOL_ERROR = "VERIFICATION_TOOL_ERROR"
    SKIPPED_NO_MAPPING = "SKIPPED_NO_MAPPING"
    SKIPPED_BY_POLICY = "SKIPPED_BY_POLICY"


TARGET_RULES: list[tuple[re.Pattern[str], EnumVerificationTarget]] = [
    (
        re.compile(r"^src/(.*/)?projection.*\.py$"),
        EnumVerificationTarget.PROJECTION_ROW_CHECK,
    ),
    (
        re.compile(r"^src/(.*/)?projector.*\.py$"),
        EnumVerificationTarget.PROJECTION_ROW_CHECK,
    ),
    (
        re.compile(r"^src/(.*/)?handler.*\.py$"),
        EnumVerificationTarget.PROJECTION_SINK_CHECK,
    ),
    (
        re.compile(r"^src/(.*/)?route.*\.py$"),
        EnumVerificationTarget.API_ROUTE_CHECK,
    ),
    (
        re.compile(r"^src/(.*/)?api.*\.py$"),
        EnumVerificationTarget.API_ROUTE_CHECK,
    ),
    (re.compile(r"^pages/api/"), EnumVerificationTarget.API_ROUTE_CHECK),
    (re.compile(r"^drizzle/"), EnumVerificationTarget.DB_MIGRATION_CHECK),
    (re.compile(r"^migrations/"), EnumVerificationTarget.DB_MIGRATION_CHECK),
    (re.compile(r"(^|.*/)topics\.yaml$"), EnumVerificationTarget.KAFKA_TOPIC_CHECK),
    (re.compile(r"(^|.*/)contract\.yaml$"), EnumVerificationTarget.KAFKA_TOPIC_CHECK),
]


def map_changed_files_to_target(changed_files: list[str]) -> EnumVerificationTarget:
    for path in changed_files:
        for pattern, target in TARGET_RULES:
            if pattern.search(path):
                return target
    return EnumVerificationTarget.SKIPPED_NO_MAPPING


def classify_verification_outcome(
    target: EnumVerificationTarget,
    exit_code: int,
    stdout: str,
    elapsed_seconds: float,
    timeout_seconds: int,
) -> EnumVerificationOutcome:
    if target == EnumVerificationTarget.SKIPPED_NO_MAPPING:
        return EnumVerificationOutcome.SKIPPED_NO_MAPPING

    if elapsed_seconds >= timeout_seconds:
        return EnumVerificationOutcome.VERIFICATION_TIMEOUT

    if exit_code != 0:
        return EnumVerificationOutcome.VERIFICATION_FAILED

    return EnumVerificationOutcome.MERGED
