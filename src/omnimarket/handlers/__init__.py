# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed handlers for local calls and server-side reconciliation.

Exports resolve on first use so that importing one submodule (for example the
test-selection policy adapter the CI selector runs in a bare venv) does not
import every handler and the packages they depend on.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .handler_lane_intake_create import HandlerLaneIntakeCreate
    from .handler_local_access_mode import HandlerLocalAccessMode

_EXPORTS = {
    "HandlerLaneIntakeCreate": ".handler_lane_intake_create",
    "HandlerLocalAccessMode": ".handler_local_access_mode",
}

__all__ = ["HandlerLaneIntakeCreate", "HandlerLocalAccessMode"]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module, __name__), name)
