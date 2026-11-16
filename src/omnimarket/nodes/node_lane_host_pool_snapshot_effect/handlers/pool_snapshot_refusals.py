# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refusal text the snapshot writer gives a host, shared with the readers of the snapshot.

A reader that tells a host whose account read failed from a host that offers nothing matches the writer's prefix, so
the writer and every reader take it from here and no test spells it again.
"""

from __future__ import annotations

# The refusal of a host whose token pool (account) read failed: its slots are 0 but unknown, not 0 free.
ACCOUNTS_UNREAD_PREFIX = "accounts-unread"

__all__: list[str] = ["ACCOUNTS_UNREAD_PREFIX"]
