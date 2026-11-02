# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three per-principal compaction reply topic templates, read from the contract (OMN-22103).

The orchestrator contract's ``published_events`` rows for the package reply,
prepared and fallback events carry ``topic_parts``; this is the one reader.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.context_compaction.model_reply_topic_template import (
    ModelReplyTopicTemplate,
)

_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "nodes"
    / "node_context_compaction_orchestrator"
    / "contract.yaml"
)
_ROWS = {
    "package": "ContextCompactionPackageReply",
    "prepared": "ContextCompactionPrepared",
    "fallback": "ContextCompactionFallback",
}


class ModelReplyTopicTemplates(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    package: ModelReplyTopicTemplate
    prepared: ModelReplyTopicTemplate
    fallback: ModelReplyTopicTemplate

    @model_validator(mode="after")
    def _one_principal_pattern(self) -> Self:
        patterns = {
            self.package.principal_pattern,
            self.prepared.principal_pattern,
            self.fallback.principal_pattern,
        }
        if len(patterns) != 1:
            raise ValueError("the reply topics declare different principal patterns")
        return self

    @property
    def principal_pattern(self) -> str:
        return self.package.principal_pattern


@cache
def load_reply_topic_templates() -> ModelReplyTopicTemplates:
    """The templates, from the orchestrator contract's ``published_events``."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    parts = {
        row["event_type"]: row["topic_parts"]
        for row in contract["published_events"]
        if "topic_parts" in row
    }
    return ModelReplyTopicTemplates(
        **{key: parts[event_type] for key, event_type in _ROWS.items()}
    )


__all__ = ["ModelReplyTopicTemplates", "load_reply_topic_templates"]
