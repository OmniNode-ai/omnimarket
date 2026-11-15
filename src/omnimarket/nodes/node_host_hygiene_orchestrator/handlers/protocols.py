# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the host hygiene orchestrator."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from omnimarket.models.model_lab_work_unit import (
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)


class ProtocolHostHygieneLabWorkSender(Protocol):
    """Sends one lab work unit to a pool host and waits for its receipt.

    Never raises for an unanswered unit: it returns an infra_error receipt.
    """

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt: ...


class ProtocolHostHoldReader(Protocol):
    """Which hosts their own capacity guard holds: host name -> the guard's reason.

    A host not in the mapping is not held. A reader that cannot read raises;
    the caller decides.
    """

    def held_hosts(self, now: datetime, max_age_seconds: int) -> Mapping[str, str]: ...
