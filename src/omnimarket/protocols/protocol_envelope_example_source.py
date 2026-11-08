# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The identity and clock the example envelope is stamped with."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolEnvelopeIdentitySource(Protocol):
    """Fresh identifiers and the current instant, bound by dependency injection."""

    def new_id(self) -> uuid.UUID: ...

    def now(self) -> dt.datetime: ...


__all__: list[str] = ["ProtocolEnvelopeIdentitySource"]
