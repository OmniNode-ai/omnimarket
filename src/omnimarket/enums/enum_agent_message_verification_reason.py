# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why a reader refuses an agent message: the typed reasons of the sender proof."""

from __future__ import annotations

from enum import StrEnum


class EnumAgentMessageVerificationReason(StrEnum):
    """The closed set of reasons a message is refused before anyone reads it."""

    MALFORMED_MESSAGE = "malformed_message"
    OVERSIZE_BODY = "oversize_body"
    UNKNOWN_SENDER = "unknown_sender"
    SENDER_MISMATCH = "sender_mismatch"
    MISSING_PROOF = "missing_proof"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
