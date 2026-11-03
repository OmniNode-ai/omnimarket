# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the codex rung: the vote request, the harness run it reads, the vote.

The vote has the shape of the dispatch orchestrator's voter review (identity, endpoint,
model, delegation event id, findings); this package keeps its own copy because a node
never imports another node's models.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_PACKAGE = "omnimarket.nodes.node_agent_review_codex_rung_effect"
CONFIG_KEY = "agent_review_codex_rung"


class ModelCodexRungConfig(BaseModel):
    """``config.agent_review_codex_rung`` of the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    harness: str = Field(min_length=1)
    login: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    call_budget_seconds: float = Field(gt=0)
    harness_script: str = Field(min_length=1)

    @classmethod
    def from_contract(cls) -> ModelCodexRungConfig:
        contract = yaml.safe_load(
            files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
        )
        return cls.model_validate(contract["config"][CONFIG_KEY])


class ModelCodexVoteRequest(BaseModel):
    """One voter's review: the roster identity and model, and the full review prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str = Field(min_length=1)
    model: str = Field(min_length=1)
    prompt: str = Field(min_length=1)


class ModelCodexFinding(BaseModel):
    """One finding the voter raised."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    line: int = Field(ge=0)
    severity: str = Field(min_length=1)
    claim: str = ""


class ModelCodexVote(BaseModel):
    """The vote: the provenance of the harness run and the findings it returned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str
    endpoint: str
    model: str
    delegation_event_id: str = Field(min_length=1)
    findings: tuple[ModelCodexFinding, ...] = ()


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
    "CONFIG_KEY",
    "ModelCodexFinding",
    "ModelCodexRungConfig",
    "ModelCodexVote",
    "ModelCodexVoteRequest",
    "ModelHarnessRun",
]
