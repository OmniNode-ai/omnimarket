# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the host hygiene orchestrator."""

from __future__ import annotations

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
