# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_post_merge_knowledge_sync_orchestrator [OMN-11930].

Contains:
- ModelPostMergeSyncRequest: input — PR merge event payload
- ModelSyncFanoutCommands: output — which backends to trigger and metadata
- ModelSyncEvidence: durable evidence record written to disk
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelPostMergeSyncRequest(BaseModel):
    """Input to the post-merge knowledge sync orchestrator.

    Describes a PR that has merged to main and the files it changed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sync_run_id: UUID
    correlation_id: UUID
    affected_repo: str = Field(description="GitHub org/repo that was merged into")
    merged_pr: int = Field(description="PR number that merged")
    source_commit_sha: str = Field(description="Merge commit SHA on main")
    changed_files: list[str] = Field(
        description="List of file paths changed by the PR (relative to repo root)"
    )
    pr_title: str = Field(default="", description="PR title for ADR keyword heuristic")
    pr_body: str = Field(default="", description="PR body for ADR keyword heuristic")


class ModelSyncFanoutCommands(BaseModel):
    """Output of the orchestrator — conditional fan-out decisions and metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sync_run_id: UUID
    affected_repo: str
    merged_pr: int
    source_commit_sha: str

    trigger_repowise_reindex: bool = Field(
        description="Always True — every merge triggers a Repowise reindex"
    )
    trigger_memgraph_repopulation: bool = Field(
        description="True iff any contract.yaml file was modified"
    )
    trigger_qdrant_antipattern_reindex: bool = Field(
        description="True iff any antipattern catalog file was modified"
    )
    trigger_kb_adr_canary: bool = Field(
        description="True iff PR title/body contains ADR-relevant keywords"
    )

    modified_contract_files: list[str] = Field(
        default_factory=list,
        description="Subset of changed_files that are contract.yaml files",
    )
    modified_antipattern_files: list[str] = Field(
        default_factory=list,
        description="Subset of changed_files that are antipattern catalog files",
    )


class ModelSyncEvidence(BaseModel):
    """Durable evidence record written to .onex_state/knowledge-sync/<sync_run_id>.json."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sync_run_id: UUID
    affected_repo: str
    merged_pr: int
    source_commit_sha: str

    trigger_repowise_reindex: bool
    trigger_memgraph_repopulation: bool
    trigger_qdrant_antipattern_reindex: bool
    trigger_kb_adr_canary: bool

    repowise_index_hash: str = Field(
        default="",
        description="Repowise index hash after reindex (empty if not triggered or unavailable)",
    )
    memgraph_snapshot_hash: str = Field(
        default="",
        description="Memgraph snapshot hash after repopulation (empty if not triggered)",
    )
    qdrant_collection_version: str = Field(
        default="",
        description="Qdrant collection version after reindex (empty if not triggered)",
    )
