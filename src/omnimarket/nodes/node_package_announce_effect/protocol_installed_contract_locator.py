# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Where an announced contract's installed file is (OMN-21597)."""

from pathlib import Path
from typing import Protocol, runtime_checkable

from omnibase_core.models.events.contract_registration.model_announced_package import (
    ModelAnnouncedPackage,
)


@runtime_checkable
class ProtocolInstalledContractLocator(Protocol):
    """Finds the installed file of one contract so admission can hash its bytes again.

    A registry that admits announcements from another process binds its own locator.
    """

    def locate(self, package: ModelAnnouncedPackage, name: str) -> Path | None:
        """Return the installed file, or ``None`` when none is known."""
        ...
