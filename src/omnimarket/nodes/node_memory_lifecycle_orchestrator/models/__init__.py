# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Memory Lifecycle Orchestrator Models.

Input, output, command, and event models for memory lifecycle orchestration.

Model Categories:
    - Input/Output: Orchestrator node I/O models
    - Commands: Explicit lifecycle transition commands
    - Events: Lifecycle transition event models

Models (to be implemented):
    - ModelLifecycleOrchestratorInput: Orchestrator input configuration
    - ModelLifecycleOrchestratorOutput: Orchestrator execution results
    - ModelArchiveMemoryCommand: Command to archive memory to cold storage
    - ModelExpireMemoryCommand: Command to explicitly expire memory
    - ModelRestoreMemoryCommand: Command to restore archived memory
    - ModelMemoryExpiredEvent: Event emitted when memory expires
    - ModelMemoryArchivedEvent: Event emitted when memory is archived
    - ModelMemoryRestoredEvent: Event emitted when memory is restored
    - ModelLifecycleTransitionFailedEvent: Event emitted on transition failure

.. versionadded:: 0.1.0
    Initial implementation for OMN-1453.

Ticket: OMN-1453
"""

__all__: list[str] = []
