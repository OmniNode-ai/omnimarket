# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_broker_topic_removal_effect: every setting of the removal, none in code.

Found at ``<root>/node_broker_topic_removal_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_broker_load_reading_effect.overlay import (
    ModelBrokerMetricsSource,
)
from omnimarket.nodes.node_topic_never_produced_compute.models.model_topic_never_produced import (
    ModelNeverProducedRule,
)

NODE_NAME = "node_broker_topic_removal_effect"


class ModelContractSource(BaseModel):
    """Where the contracts that declare topics are read, and how a topic and its role are told."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Environment variable naming the registry root that holds the repositories.
    registry_root_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    #: Repositories under the registry root whose contracts declare topics.
    repositories: tuple[str, ...] = Field(min_length=1)
    #: Glob, relative to a repository, that finds the contract files.
    contract_glob: str = Field(min_length=1)
    #: A string is a topic name when it fully matches this.
    topic_pattern: str = Field(min_length=1)
    #: A topic under a key whose name holds one of these fragments is declared as published.
    publish_key_fragments: tuple[str, ...] = Field(min_length=1)
    #: Path parts that are never contract roots (vendored trees, worktrees, caches).
    excluded_parts: tuple[str, ...]

    @field_validator("topic_pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class ModelTopicRemovalOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics_source: ModelBrokerMetricsSource
    contracts: ModelContractSource
    #: The never-produced listing's own rule: protected prefixes and the declared-producer policy.
    listing_rule: ModelNeverProducedRule
    #: Whether a never-produced topic that any contract declares may be a candidate. False refuses it.
    remove_declared_topics: bool
    #: Whether an execute run may issue deletes at all; a dry run never reads it.
    execute_enabled: bool
    #: The most topics one execute run may remove; a plan over it is flagged and an execute run refused.
    max_topics_per_run: int = Field(ge=1)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelTopicRemovalOverlay:
    return load_structured_overlay(NODE_NAME, ModelTopicRemovalOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelContractSource",
    "ModelTopicRemovalOverlay",
    "load_overlay",
]
