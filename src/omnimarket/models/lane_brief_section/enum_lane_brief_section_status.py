# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome of rendering a lane brief's section 3a (OMN-21118, plan task TA.17)."""

from enum import StrEnum


class EnumLaneBriefSectionStatus(StrEnum):
    """A refusal never renders a partial section 3a."""

    RENDERED = "rendered"
    REFUSED = "refused"
