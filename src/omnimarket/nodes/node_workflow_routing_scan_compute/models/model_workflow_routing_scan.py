# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the workflow routing scan node (OMN-22948)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRoutingSelection(StrEnum):
    """What one scenario of a job's ``runs-on`` selects."""

    HOSTED_LABEL = "hosted_label"
    LAB_LABEL = "lab_label"
    RUNNER_GROUP = "runner_group"
    COMPUTED = "computed"
    UNDECLARED_VARIABLE = "undeclared_variable"
    UNREAD_SEAM_VARIABLE = "unread_seam_variable"
    SEAM_VARIABLE = "seam_variable"
    CALLER = "caller"
    UNREAD_CALLEE = "unread_callee"
    UNPARSEABLE = "unparseable"
    UNREAD_REPOSITORY = "unread_repository"


class EnumRoutingResolution(StrEnum):
    """How a scenario ends: on a hosted label, moved to a lab job node, deleted, or not yet resolved."""

    HOSTED = "hosted"
    MOVED_TO_NODE = "moved_to_node"
    DELETED = "deleted"
    UNRESOLVED = "unresolved"


class EnumRoutingVia(StrEnum):
    """Whether the definition itself resolves the scenario or a declared route does."""

    DEFINITION = "definition"
    ROUTE = "route"
    NONE = "none"


class EnumRoutingRouteKind(StrEnum):
    """The kinds a declared route takes, the ones of the typed public-lab route list."""

    MOVES_TO_HOSTED = "moves_to_hosted"
    LAB_JOB_NODE = "lab_job_node"
    RETIRES_WITH_RUNNERS = "retires_with_runners"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelWorkflowFile(_Frozen):
    path: str
    source: str


class ModelScannedRepository(_Frozen):
    """One repository's workflow definitions, read by the caller from its default branch."""

    name: str
    visibility: str = "unknown"
    workflows: tuple[ModelWorkflowFile, ...] = ()


class ModelRoutingRoute(_Frozen):
    """A declared resolution of one job (the typed route list the caller reads and injects)."""

    repository: str
    workflow: str
    job: str
    kind: EnumRoutingRouteKind


class ModelRepositoryVariable(_Frozen):
    """The live value of one Actions variable of one repository, as the caller read it."""

    repository: str
    name: str
    value: str


class ModelRoutingScanPolicy(_Frozen):
    """Every setting of the scan; the node's overlay file is this model in YAML."""

    hosted_label_pattern: str
    hosted_seam_variables: tuple[str, ...]
    self_hosted_marker: str
    variable_reference_pattern: str
    unprovable_context_pattern: str
    matrix_reference_pattern: str
    non_label_call_pattern: str
    returned_literal_pattern: str
    json_array_literal_pattern: str
    uses_pattern: str


class ModelWorkflowRoutingScanRequest(_Frozen):
    """``expected_repositories`` is the set a complete read must cover; the caller reads it from its private overlay home."""

    policy: ModelRoutingScanPolicy
    expected_repositories: tuple[str, ...]
    repositories: tuple[ModelScannedRepository, ...]
    routes: tuple[ModelRoutingRoute, ...] = ()
    variables: tuple[ModelRepositoryVariable, ...] = ()


class ModelRoutingScenario(_Frozen):
    """One scenario of one job and what resolves it; a repository never read is one scenario of its own."""

    repository: str
    workflow: str
    job: str
    selection: EnumRoutingSelection
    detail: str
    resolution: EnumRoutingResolution
    via: EnumRoutingVia


class ModelStaleRoute(_Frozen):
    """A declared route whose job is no longer in the definitions read."""

    repository: str
    workflow: str
    job: str


class ModelRepositorySummary(_Frozen):
    name: str
    visibility: str
    read: bool
    workflows: int
    scenarios: int
    unresolved: int
    pending_execution: int


class ModelWorkflowRoutingScanResult(_Frozen):
    """The scan's whole reading: every scenario, the counts, and what was never read."""

    complete: bool
    repositories_expected: int
    repositories_read: int
    repositories_unread: tuple[str, ...] = ()
    repositories_extra: tuple[str, ...] = ()
    workflows_read: int
    scenario_count: int
    unresolved_count: int
    pending_execution_count: int
    scenarios: tuple[ModelRoutingScenario, ...] = Field(default_factory=tuple)
    repositories: tuple[ModelRepositorySummary, ...] = ()
    stale_routes: tuple[ModelStaleRoute, ...] = ()
