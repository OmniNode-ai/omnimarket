# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the capability-catalog publish handler"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelCapabilityCatalogPublishResult:
    """The run's exit code: 0 published or nothing to publish, 1 a git or GitHub step failed."""

    exit_code: int
