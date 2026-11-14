# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Errors for the release sibling premise compute node."""


class SiblingPremiseInputError(ValueError):
    """Raised when the request's candidate_merged_at is later than as_of."""


__all__ = [
    "SiblingPremiseInputError",
]
