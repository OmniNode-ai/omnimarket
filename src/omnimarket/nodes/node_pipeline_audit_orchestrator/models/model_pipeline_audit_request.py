# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_pipeline_audit_orchestrator [OMN-12211].

ModelPipelineAuditRequest: carries the repo list, audit type, and execution
flags consumed by the orchestrator when triggered via
onex.cmd.omnimarket.pipeline-audit-start.v1.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumAuditType(StrEnum):
    """Scope of the pipeline audit run."""

    FULL = "full"
    """All six proof categories across all repos."""

    TOPICS = "topics"
    """Wire topics table only (producer/consumer byte-for-byte match)."""

    SCHEMA = "schema"
    """DSN proof + schema handshake only."""

    ENTRYPOINT = "entrypoint"
    """Runtime entrypoint proof only."""

    WIRE_FORMAT = "wire_format"
    """Wire format compatibility (Pydantic model field comparison) only."""

    CORRELATION = "correlation"
    """Correlation ID threading only."""


class EnumPipelineSize(StrEnum):
    """Pipeline size hint; controls agent dispatch strategy."""

    SMALL = "small"
    """2-4 repos, 1-3 integration points — single agent handles all phases."""

    MEDIUM = "medium"
    """5-8 repos, 4-8 integration points — one agent per repo in Phase 2."""

    LARGE = "large"
    """9+ repos, 9+ integration points — batched parallel agents per phase."""


class ModelPipelineAuditRequest(BaseModel):
    """Input to the pipeline audit orchestrator.

    All flags mirror the /pipeline-audit skill surface defined in
    omniclaude/plugins/onex/skills/pipeline_audit/SKILL.md.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[str, ...] = Field(
        description=(
            "List of repo names or absolute paths to include in the audit. "
            "When empty, the orchestrator performs auto-discovery from the "
            "parent directory."
        ),
    )
    audit_type: EnumAuditType = Field(
        default=EnumAuditType.FULL,
        description="Scope of proof categories to apply.",
    )
    parallel: bool = Field(
        default=True,
        description=(
            "Dispatch per-repo audit agents in parallel (Phase 2). "
            "Set False for sequential execution during debugging."
        ),
    )
    pipeline_size: EnumPipelineSize = Field(
        default=EnumPipelineSize.MEDIUM,
        description="Pipeline size hint controlling agent dispatch strategy.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Perform discovery and inventory without running proof categories. "
            "Returns Phase 1-2 output only."
        ),
    )
    skip_ticket_creation: bool = Field(
        default=False,
        description=(
            "Compile gap register (Phase 5) but skip Phase 6 ticket creation. "
            "Useful when auditing without Linear write access."
        ),
    )
    fail_fast: bool = Field(
        default=False,
        description="Abort audit on the first BREAKING finding.",
    )
    omni_home_path: str = Field(
        default="",
        description=(
            "Absolute path to the omni_home checkout. When empty, resolved "
            "from the OMNI_HOME environment variable."
        ),
    )
