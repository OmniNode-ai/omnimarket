# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four kinds of file that put an automatic process on a host (OMN-20804)."""

from __future__ import annotations

from enum import StrEnum


class EnumAutomationLivenessSourceClass(StrEnum):
    """What a scanned file is."""

    #: A systemd ``.service`` or ``.timer`` file.
    SYSTEMD_UNIT_FILE = "systemd_unit_file"
    #: A launchd property list or a template of one.
    LAUNCHD_PLIST = "launchd_plist"
    #: A file under a ``cron.d`` directory.
    CRON_D_FILE = "cron_d_file"
    #: A drift manifest that names the units a host is expected to run.
    UNIT_MANIFEST = "unit_manifest"
    #: Code that writes a launchd agent, a systemd unit, a crontab or a cron.d file.
    INSTALLER_CODE = "installer_code"
