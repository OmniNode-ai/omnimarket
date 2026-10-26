# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RSD migration orchestrator node (OMN-21373).

One script and an omnimarket worktree in; a new node in canonical shape, its
registered entry point and a migration receipt out. The dispatch runs only
under the task class the registry's ``task_class_authority`` declares, and the
work is one delegated code edit loop.
"""

from omnimarket.nodes.node_rsd_migration_orchestrator.handlers.handler_rsd_migration_orchestrator import (
    HandlerRsdMigrationOrchestrator,
    RsdDispatchRefusedError,
    canonical_shape_problems,
    resolve_dispatch_authority,
    run_rsd_migration,
)
from omnimarket.nodes.node_rsd_migration_orchestrator.models.model_rsd_migration import (
    ModelRsdDispatchAuthority,
    ModelRsdMigrationReceipt,
    ModelRsdMigrationTask,
)

__all__ = [
    "HandlerRsdMigrationOrchestrator",
    "ModelRsdDispatchAuthority",
    "ModelRsdMigrationReceipt",
    "ModelRsdMigrationTask",
    "RsdDispatchRefusedError",
    "canonical_shape_problems",
    "resolve_dispatch_authority",
    "run_rsd_migration",
]
