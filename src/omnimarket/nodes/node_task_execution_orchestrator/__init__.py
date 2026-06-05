# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_task_execution_orchestrator — generic task.execute route planner.

First vertical slice (OMN-12702). Normalizes a raw prompt or a fully formed
ModelTaskContract into one ModelTaskContract, then deterministically maps each
requirement and mechanical DoD check to an existing route NAME (delegation,
verification) WITHOUT executing it. Dry-run only — no side effects.

task.execute COMPOSES existing authorities; it must not become a new authority.
No new envelope and no new DoD model are introduced: ModelTaskContract,
ModelMechanicalCheck, ModelDispatchBusCommand, and ModelDispatchBusTerminalResult
are reused verbatim from omnibase_core.
"""
