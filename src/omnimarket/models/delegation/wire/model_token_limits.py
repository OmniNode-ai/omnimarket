# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Delegation token ceilings shared by wire and adapter request models."""

DELEGATION_DEFAULT_MAX_TOKENS = 8192
DELEGATION_MAX_TOKENS_HARD_LIMIT = 8192

__all__: list[str] = [
    "DELEGATION_DEFAULT_MAX_TOKENS",
    "DELEGATION_MAX_TOKENS_HARD_LIMIT",
]
