# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local environment projection: its pure plan, its bus writer, and the store-neutral write.

The store-neutral write lives in ``omnimarket.handlers`` so the dashboard's
native report and this projection write through one seam without either
importing the other.
"""

from omnimarket.handlers.handler_local_environment_store import (
    LOCAL_ENVIRONMENT_KEY,
    LOCAL_ENVIRONMENT_TABLE,
    apply_local_environment_status,
    plan_local_environment_write,
)

from .handler_local_environment_writer import LocalEnvironmentProjectionWriter
from .handler_projection_local_environment import HandlerProjectionLocalEnvironment

__all__ = [
    "LOCAL_ENVIRONMENT_KEY",
    "LOCAL_ENVIRONMENT_TABLE",
    "HandlerProjectionLocalEnvironment",
    "LocalEnvironmentProjectionWriter",
    "apply_local_environment_status",
    "plan_local_environment_write",
]
