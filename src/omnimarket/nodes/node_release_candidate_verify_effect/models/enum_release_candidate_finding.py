# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The kinds of mismatch the release candidate verifier names."""

from __future__ import annotations

from enum import StrEnum


class EnumReleaseCandidateFinding(StrEnum):
    ENVIRONMENT_UNREADABLE = "environment_unreadable"
    DISTRIBUTION_MISSING = "distribution_missing"
    DISTRIBUTION_VERSION = "distribution_version"
    FILE_MISSING = "file_missing"
    FILE_MODIFIED = "file_modified"
    ENTRY_POINT_EXTRA = "entry_point_extra"
    ENTRY_POINT_MISSING = "entry_point_missing"
    ENTRY_POINT_REDIRECTED = "entry_point_redirected"
    PROTECTED_SCRIPT_OWNER = "protected_script_owner"
    PROTECTED_SCRIPT_TARGET = "protected_script_target"
    LAUNCHER_OUTSIDE_ENVIRONMENT = "launcher_outside_environment"
    LAUNCHER_LAYOUT = "launcher_layout"
    LAUNCHER_DIFFERS = "launcher_differs"
    PLUGIN_VERSION = "plugin_version"
    PLUGIN_FILE_MISSING = "plugin_file_missing"
    PLUGIN_FILE_MODIFIED = "plugin_file_modified"
    PLUGIN_EXTRA_FILE = "plugin_extra_file"
    PYPI_FACT_MISSING = "pypi_fact_missing"
    PYPI_WHEEL_SHA256 = "pypi_wheel_sha256"
    PYPI_RECORD = "pypi_record"
    CHANGE_NOT_IN_CANDIDATE = "change_not_in_candidate"
