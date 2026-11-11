# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One candidate after the run leg read it (OMN-23627, plan rules 1, 3, 4 and 8).

Exemplar: ``nodes/node_pr_landing_orchestrator/models/``. The material of a task
is a pure function of its candidate (repository, merge commit, contract id), so
the orchestrator re-reads it for each send instead of holding it in the row: the
row keeps only the digest over every task's material hashes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_code_contract_score_effect.models.model_code_score import (
    ModelCodeScoreCheckFile,
    ModelCodeScoreContractRef,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.models.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)


def task_key_for(candidate: ModelCodeEvalCandidate) -> str:
    """The stable key of a candidate: its contract id and the head of its merge commit."""
    return f"{candidate.contract_id}-{candidate.merge_commit[:12]}"


class ModelCodeEvalTaskMaterial(BaseModel):
    """What a task needs to be drafted and scored; never sent in a draft's check list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_key: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    #: Title, description and each item's description of the contract; never a check value.
    task_text: str
    #: The held answer's changed paths: what the loop may write (never a check file).
    writable_globs: tuple[str, ...]
    contract: ModelCodeScoreContractRef
    check_files: tuple[ModelCodeScoreCheckFile, ...] = ()
    #: The merged change's diff with the contract and check files taken out; the control's draft.
    held_diff: str
    band: str = Field(min_length=1)
    #: SHA-256 over the task text, check files and held diff (the suite digest reads it).
    material_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: Set when the task is not admitted (rule 3): the reason it is excluded, counted by reason.
    excluded_reason: str | None = None


__all__ = ["ModelCodeEvalTaskMaterial", "task_key_for"]
