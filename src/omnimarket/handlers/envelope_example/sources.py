# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production identity source: random ids and the system clock."""

from __future__ import annotations

import datetime as dt
import uuid


class SystemEnvelopeIdentitySource:
    def new_id(self) -> uuid.UUID:
        return uuid.uuid4()

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)


__all__ = ["SystemEnvelopeIdentitySource"]
