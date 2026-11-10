# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ports of the rotation tick: the host's login file and the one source of marks (OMN-23000).

Production wires ``handlers.adapter_rotation_ports``; tests wire a real temporary file and a fake source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import SecretStr

from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationMarksRead,
)


class ProtocolLoginFile(Protocol):
    """The login a host's lanes read. The value is handed to the handler only, never returned in a result."""

    def fingerprint(self) -> str | None:
        """The pool fingerprint of the login now on the host, ``None`` when there is no login."""
        ...

    def since(self) -> datetime | None:
        """When the login now on the host was written, ``None`` when there is no login."""
        ...

    def write(self, value: SecretStr) -> None:
        """Replace the login (mode 0600), keeping the previous one as ``.prev``."""
        ...


class ProtocolMarksSource(Protocol):
    def read(self) -> ModelRotationMarksRead: ...
