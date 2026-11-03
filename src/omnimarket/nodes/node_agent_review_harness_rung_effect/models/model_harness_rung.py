# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the harness rung: the rung config, the vote request, the run, the vote.

One node serves every harness rung. The rung's config (harness, login, endpoint, task
class, effort, call budget, harness script and run-directory leaf) is data: the dispatch
reads it from its overlay and sends it in each vote request, so no harness is named in
code. The vote has the shape of the dispatch orchestrator's voter review (identity,
endpoint, model, delegation event id, findings); this package keeps its own copy because
a node never imports another node's models.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelHarnessRungConfig(BaseModel):
    """One rung's harness: what to run and what its receipt must show."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    harness: str = Field(min_length=1)
    login: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    call_budget_seconds: float = Field(gt=0)
    harness_script: str = Field(min_length=1)
    run_dir_leaf: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")


class ModelHarnessVoteRequest(BaseModel):
    """One voter's review: the roster identity and model, the rung config, the prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str = Field(min_length=1)
    model: str = Field(min_length=1)
    rung: ModelHarnessRungConfig
    prompt: str = Field(min_length=1)


class ModelHarnessFinding(BaseModel):
    """One finding the voter raised."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(ge=0)
    severity: str = Field(min_length=1)
    claim: str = ""


class ModelHarnessVote(BaseModel):
    """The vote, unsigned: the provenance of the harness run and the findings it returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    endpoint: str
    model: str
    delegation_event_id: str = Field(min_length=1)
    findings: tuple[ModelHarnessFinding, ...] = ()


class ModelHarnessRun(BaseModel):
    """What one harness run left: its receipt and its answer text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt: dict[str, Any]
    answer: str = ""

    def field(self, *path: str) -> Any:
        node: Any = self.receipt
        for key in path:
            if not isinstance(node, Mapping):
                return None
            node = node.get(key)
        return node


__all__ = [
    "ModelHarnessFinding",
    "ModelHarnessRun",
    "ModelHarnessRungConfig",
    "ModelHarnessVote",
    "ModelHarnessVoteRequest",
]
