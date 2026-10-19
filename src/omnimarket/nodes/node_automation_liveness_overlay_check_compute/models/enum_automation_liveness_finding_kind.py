# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a completeness finding says is wrong (OMN-20804)."""

from __future__ import annotations

from enum import StrEnum


class EnumAutomationLivenessFindingKind(StrEnum):
    """One reason the overlay and the installers it must cover disagree."""

    #: The overlay does not load: a broken deadline, a cron entry with no completion record,
    #: a duplicate id, a missing field. The detail carries the seam's own message.
    OVERLAY_INVALID = "overlay_invalid"
    #: A positive control that cannot prove its own entry: a class drill for another trigger
    #: kind, or a replay fixture outside the replay namespace.
    POSITIVE_CONTROL_MISMATCH = "positive_control_mismatch"
    #: The sources declaration does not load.
    SOURCES_INVALID = "sources_invalid"
    #: A repository the declaration names was not scanned, so nothing is known about it.
    REPOSITORY_NOT_SCANNED = "repository_not_scanned"
    #: The scan found a unit, template, cron file or installer that no declaration covers.
    UNDECLARED_SOURCE = "undeclared_source"
    #: A declared source the scan no longer finds: deleted, or no longer an installer.
    STALE_SOURCE = "stale_source"
    #: A source installs a unit that no overlay entry declares.
    UNDECLARED_UNIT = "undeclared_unit"
    #: An entry is ``not_installed`` yet no installer installs it.
    NOT_INSTALLED_WITHOUT_INSTALLER = "not_installed_without_installer"
    #: An entry is ``retired`` yet an installer still installs it.
    RETIRED_STILL_INSTALLED = "retired_still_installed"
