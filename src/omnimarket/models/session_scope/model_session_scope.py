# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger principals a recovering session may act for.

Bound by :func:`omnimarket.handlers.session_scope.bind_session_scope` through the team identity
declaration; shared by the session resume and session checkpoint nodes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelSessionScope(BaseModel):
    """The principals a session acts for. ``privileges`` is ``team`` unless the operator matched."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    privileges: Literal["operator", "team"]
    entry_id: str | None
    principals: tuple[str, ...]
    # The principal this session records on what it writes (a checkpoint); None when unbound.
    writer_principal: str | None
    diagnostic: str | None
    source_sha256: str | None

    @property
    def is_operator(self) -> bool:
        return self.privileges == "operator"

    def owns(self, principal: str | None) -> bool:
        """Whether this session may act on something ``principal`` wrote.

        An operator session owns everything, as before the team existed. A member session owns only
        its own principals; a missing principal is unverified and never owned.
        """
        if self.is_operator:
            return True
        return principal is not None and principal in self.principals
