# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which ledger principals a recovering session may act for, resolved through the team identity node."""

from .binding import ModelSessionScope, bind_session_scope

__all__ = ["ModelSessionScope", "bind_session_scope"]
