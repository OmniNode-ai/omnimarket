# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enumerations for contract registration materialization."""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumMaterializationStatus(StrEnum):
    """Outcome of a contract registration request."""

    MATERIALIZED = "materialized"
    ALREADY_MATERIALIZED = "already_materialized"
    REJECTED = "rejected"


@unique
class EnumMaterializationRejection(StrEnum):
    """Reason a contract registration was rejected."""

    PARSE_FAILURE = "parse_failure"
    HASH_MISMATCH = "hash_mismatch"
    HANDLER_ALLOWLIST = "handler_allowlist"
    PROFILE_MISMATCH = "profile_mismatch"
    VERSION_CONFLICT = "version_conflict"


__all__ = ["EnumMaterializationRejection", "EnumMaterializationStatus"]
