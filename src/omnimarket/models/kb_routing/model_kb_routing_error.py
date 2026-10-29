# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The error every knowledge-routing rules load failure raises."""

from __future__ import annotations


class KbRoutingRulesError(ValueError):
    """The rules, an overlay or a manifest are invalid; the load fails."""


__all__ = ["KbRoutingRulesError"]
