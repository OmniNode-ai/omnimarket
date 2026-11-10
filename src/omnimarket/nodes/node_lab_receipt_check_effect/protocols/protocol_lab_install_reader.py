# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab install read seam of node_lab_receipt_check_effect (OMN-21826)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnibase_core.models.lab_receipt.model_lab_receipt_lab_read import (
    ModelLabReceiptLabRead,
)


@runtime_checkable
class ProtocolLabInstallReader(Protocol):
    """Ask lab hosts what they report installed and whether it is running (verify phase only)."""

    async def read_installs(
        self, hosts: tuple[str, ...]
    ) -> tuple[ModelLabReceiptLabRead, ...]:
        """One read per named host.

        Raises when the lab cannot be reached; the caller then supplies no lab read at all and the
        validator refuses the run, so an unreachable lab is never a pass.
        """
        ...
