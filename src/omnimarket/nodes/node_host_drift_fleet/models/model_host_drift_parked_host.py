# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host the lab host table parks: listed in the view, never required to report."""

from ._base import ModelFrozen


class ModelHostDriftParkedHost(ModelFrozen):
    """One parked overlay host and the table's reason for parking it."""

    host: str
    reason: str
