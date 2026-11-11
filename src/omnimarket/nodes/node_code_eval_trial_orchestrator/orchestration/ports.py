# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports of the code eval trial orchestrator's legs (OMN-23627).

Exemplar: ``nodes/node_pr_landing_orchestrator/orchestration/ports.py``. A leg
reads three things the reducer must not: the clock, the pool's free hosts, and a
task's material (the run leg's reads). Each is a port with an in-memory default
for tests and a real implementation the contract wires.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_task_material import (
    ModelCodeEvalTaskMaterial,
)


@runtime_checkable
class ProtocolCodeEvalSuiteReader(Protocol):
    """The run leg's reads: a candidate's base commit and material (rules 1, 3, 4, 8).

    Pure in the candidate: the same candidate gives the same material, so the
    orchestrator re-reads a task for each send and holds only the digest.
    """

    def read(
        self, candidates: Sequence[ModelCodeEvalCandidate]
    ) -> tuple[ModelCodeEvalTaskMaterial, ...]: ...


@runtime_checkable
class ProtocolCodeEvalFreeHosts(Protocol):
    """The hosts that can take a try now; each chosen host leaves the leg's snapshot."""

    def free_hosts(
        self, now: datetime, need_tools: Sequence[str]
    ) -> tuple[str, ...]: ...

    def record(self, advertisement: object) -> None:
        """Keep a capacity advertisement (the capacity route)."""
        ...


@runtime_checkable
class ProtocolCodeEvalClock(Protocol):
    def __call__(self) -> datetime: ...


__all__ = [
    "ProtocolCodeEvalClock",
    "ProtocolCodeEvalFreeHosts",
    "ProtocolCodeEvalSuiteReader",
]
