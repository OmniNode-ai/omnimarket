# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effects supplied by the standing-rules deployment overlay."""

from typing import Protocol, runtime_checkable

from omnimarket.models.standing_rules.model_standing_rules import (
    ModelStandingRulesAudit,
)


@runtime_checkable
class ProtocolStandingRulesIO(Protocol):
    def read(self, path: str) -> str: ...

    def append_once(
        self, path: str, key: str, row: ModelStandingRulesAudit
    ) -> None: ...
