# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Subsystem classification for a runtime error (OMN-18770).

Deliberately the same seven values omnibase_infra's producer-side enum carries,
so a row this reducer writes and a row the legacy triage effect wrote describe
the same vocabulary. The DERIVATION is not shared, and must not be: the
producer's version answered from the logger name alone and answered ``unknown``
for 100% of the 69 rows on the lab surface.
"""

from __future__ import annotations

from enum import StrEnum


class EnumRuntimeErrorCategory(StrEnum):
    """Which subsystem a runtime error came out of."""

    KAFKA_CONSUMER = "kafka_consumer"
    KAFKA_PRODUCER = "kafka_producer"
    DATABASE = "database"
    HTTP_CLIENT = "http_client"
    HTTP_SERVER = "http_server"
    RUNTIME = "runtime"
    UNKNOWN = "unknown"


__all__ = ["EnumRuntimeErrorCategory"]
