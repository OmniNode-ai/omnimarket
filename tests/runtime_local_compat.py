# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test-only compatibility imports for the RuntimeLocal ownership migration."""

from __future__ import annotations

try:
    from omnibase_infra.runtime.runtime_local import RuntimeLocal
except ModuleNotFoundError:
    from omnibase_core.runtime.runtime_local import RuntimeLocal

try:
    from omnibase_infra.runtime.runtime_local_adapter import LocalRuntimeBusAdapter
except ModuleNotFoundError:
    from omnibase_core.runtime.runtime_local_adapter import LocalRuntimeBusAdapter

__all__ = ["LocalRuntimeBusAdapter", "RuntimeLocal"]
