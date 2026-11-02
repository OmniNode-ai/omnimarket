# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Remembers where each announced contract's installed file is (OMN-21597)."""

from pathlib import Path

from omnibase_core.models.events.contract_registration.model_announced_package import (
    ModelAnnouncedPackage,
)


class InstalledContractIndex:
    """Locates installed contract files by package version and contract name.

    The announcer registers a file when it reads it; admission asks for the same
    file to check that its bytes still hash to what was announced. A registry that
    admits announcements from another process binds its own locator instead.
    """

    def __init__(self) -> None:
        self._paths: dict[tuple[str, str, str], Path] = {}

    def register(self, package: ModelAnnouncedPackage, name: str, path: Path) -> None:
        """Record the installed file of one contract."""
        self._paths[(package.distribution, package.version, name)] = path

    def locate(self, package: ModelAnnouncedPackage, name: str) -> Path | None:
        """Return the installed file, or ``None`` when none was registered."""
        return self._paths.get((package.distribution, package.version, name))
