# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The environment a handler hands to the child process it starts (OMN-21595)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolProcessEnvironment(Protocol):
    """One read of the child-process environment the lane overlay binds."""

    def environment(self) -> dict[str, str]: ...


__all__: list[str] = ["ProtocolProcessEnvironment"]
