# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the selection replay: the selector replayed over merged history.

The replay input is a list of merges (sha, changed files, the declared data files' texts) and a frozen
tree snapshot identified by its git tree id. The snapshot holds everything the selector reads from a
checkout (the policy, the adjacency map, the recorded durations and the import-graph facts), so the
same input answers the same way on any checkout.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_test_selection_selector_compute.models.model_file_level_closure import (
    EnumRootConftestEdge,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyMap,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
)

_HEX40 = r"^[0-9a-f]{40}$"
_HEX64 = r"^[0-9a-f]{64}$"
#: Unread by the selector; a frozen policy that never declared a tolerance gets one inside the field's range.
_LEGACY_RECORD_TOLERANCE_RATIO = 0.5


class EnumReplayKind(StrEnum):
    NARROWED = "narrowed"
    FULL_SUITE = "full_suite"


class ModelSnapshotFile(BaseModel):
    """What the import graph reads of one file: the modules it refers to and the node names it spells."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refs: tuple[str, ...] = ()
    """Dotted names of the tree's modules the file imports or spells (resolved, sorted)."""
    relative_refs: tuple[str, ...] = ()
    """The refs a test file's text does not spell (it reaches them by a relative import), sorted."""
    names: tuple[str, ...] = ()
    """Node package names (``node_*``) the file's text spells, sorted."""


class ModelTreeSnapshot(BaseModel):
    """A checkout reduced to what the selector reads, identified by its git tree id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tree_id: str = Field(pattern=_HEX40)
    adjacency: ModelAdjacencyMap
    policy: ModelSelectorPolicy
    durations: dict[str, float]
    dirs: tuple[str, ...]
    """Every directory under ``src/omnimarket`` and ``tests``, repo-relative, sorted."""
    files: dict[str, ModelSnapshotFile]
    """Every ``.py`` file under ``src/omnimarket`` and ``tests``."""
    yaml_names: dict[str, tuple[str, ...]]
    """Node names spelled by each YAML file of a node-scoped module's node packages."""

    @field_validator("policy", mode="before")
    @classmethod
    def _legacy_policy_fields(cls, value: Any) -> Any:
        """Read a frozen snapshot's policy that predates the narrowed-run bound fields.

        A snapshot is frozen at a tree id, so one committed before ``narrowed_bound_seconds``,
        ``untimed_file_seconds``, ``record_tolerance_ratio`` and ``comparison_bounds_seconds`` existed
        still names the policy it was taken under. Each missing field takes the value that leaves that
        policy's decisions as they were: the bound is the shard cap's own reach, an untimed file costs
        nothing and no comparison bound is priced. The live policy file declares every field and
        ``ModelSelectorPolicy`` still requires them, so this only widens what a frozen snapshot may omit.
        """
        if not isinstance(value, dict) or "narrowed_bound_seconds" in value:
            return value
        return {
            **value,
            "narrowed_bound_seconds": value["max_narrowed_shards"]
            * value["target_shard_seconds"],
            "untimed_file_seconds": 0.0,
            "record_tolerance_ratio": _LEGACY_RECORD_TOLERANCE_RATIO,
            "comparison_bounds_seconds": (),
        }


class ModelDataFileTexts(BaseModel):
    """One declared data file at a merge's base and head ('' when absent on that side).

    The texts are the whole file, or only the lines that differ under their table headers: the replay reports
    the tables whose lines differ, which either form gives alike.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    base_text: str
    head_text: str


class ModelReplayMerge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str = Field(pattern=_HEX40)
    changed_files: tuple[str, ...]
    data_files: tuple[ModelDataFileTexts, ...] = ()


class ModelReplayClosure(BaseModel):
    """The file-level closure's settings for a replay of a stage at or above the closure's own."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_level_closure_stage: int = Field(ge=1, le=4)
    root_conftest_edge: EnumRootConftestEdge


class ModelReplayRequest(BaseModel):
    """One replay: the merges, the frozen tree they are replayed against, and the labels of the report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["replay_selection"] = "replay_selection"
    stage: int = Field(default=0, ge=0, le=4)
    """The selection stage replayed: 0 (the module-level selector) or the closure's stage or above."""
    closure: ModelReplayClosure | None = None
    """The file-level closure's settings; required above stage 0, because the frozen snapshot predates them."""
    flag_enabled: bool = True
    full_split_count: int = Field(ge=1, le=40)
    """Shards of a full-suite decision."""
    event_name: str = "pull_request"
    ref_name: str = "dev"
    tree_id: str = Field(pattern=_HEX40)
    snapshot_path: str | None = None
    """Repo-relative path of the snapshot file (JSON, or gzip-compressed JSON); exclusive with ``snapshot``."""
    snapshot_sha256: str | None = Field(default=None, pattern=_HEX64)
    snapshot: ModelTreeSnapshot | None = None
    repo_root: str = Field(default=".", min_length=1)
    infrastructure_categories: dict[str, str]
    """Column of the cross-tab each ``test_infrastructure_paths`` entry belongs to."""
    data_file_paths: tuple[str, ...] = ("pyproject.toml",)
    """The declared data files whose changed sections are reported."""
    merges: tuple[ModelReplayMerge, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _stage_has_a_rule_set(self) -> ModelReplayRequest:
        if self.stage == 0:
            return self
        if self.closure is None:
            raise ValueError("a replay above stage 0 needs the closure settings")
        if self.stage < self.closure.file_level_closure_stage:
            raise ValueError(
                "a replay stage is 0 or at least the closure's file_level_closure_stage"
            )
        return self

    @model_validator(mode="after")
    def _one_snapshot_source(self) -> ModelReplayRequest:
        if (self.snapshot is None) == (self.snapshot_path is None):
            raise ValueError("exactly one of snapshot and snapshot_path is required")
        if self.snapshot_path is not None and self.snapshot_sha256 is None:
            raise ValueError("snapshot_path requires snapshot_sha256")
        if self.snapshot is not None and self.snapshot.tree_id != self.tree_id:
            raise ValueError("snapshot.tree_id differs from tree_id")
        return self


class ModelReplayTrigger(BaseModel):
    """One rule a merge hit: its reason code and the changed path (or modules) that set it off."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    evidence: str
    entry: str | None = None
    """The ``test_infrastructure_paths`` entry or the protected glob the evidence matched."""


class ModelReplayDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str
    changed_files: tuple[str, ...]
    kind: EnumReplayKind
    reason_code: str
    """``narrowed``, or the full-suite reason value."""
    primary_reason: str | None
    """The full-suite reason that decided it; None for a narrowed merge."""
    triggers: tuple[ModelReplayTrigger, ...]
    selected_paths: tuple[str, ...]
    split_count: int = Field(ge=1)
    selected_seconds: float = Field(ge=0)
    """Recorded seconds of the tests the decision runs (the whole suite's for a full-suite decision)."""
    narrowed_estimate_seconds: float | None = None
    """The narrowed selection's own estimate, set when it was found too heavy."""


class ModelInfrastructureRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sha: str
    entries: tuple[str, ...]
    """The ``test_infrastructure_paths`` entries the merge's changed files matched."""
    categories: tuple[str, ...]
    cell: str
    """The categories joined with ``+``: the merge's cell of the cross-tab."""
    changed_sections: dict[str, tuple[str, ...]]
    """Changed section headers per declared data file."""


class ModelInfrastructureCrossTab(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    merges: int = Field(ge=0)
    rows: tuple[ModelInfrastructureRow, ...]
    cells: dict[str, int]


class ModelReplayTotals(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    merges: int
    narrowed: int
    full_suite: int
    reason_counts: dict[str, int]
    """Full-suite merges by primary reason."""
    selected_seconds: float
    full_suite_seconds: float
    """What the same merges would have run with no narrowing: the suite's seconds per merge."""
    selected_shards: int


class ModelReplayResult(BaseModel):
    """The counterfactual: what today's selector decides for every merge over the frozen tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: int
    tree_id: str
    snapshot_sha256: str | None
    suite_seconds: float
    decisions: tuple[ModelReplayDecision, ...]
    infrastructure_cross_tab: ModelInfrastructureCrossTab
    totals: ModelReplayTotals


class ModelReplayFailed(BaseModel):
    """Published when a replay cannot be run (an unreadable snapshot, or one that is not the named tree)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str
    terminal_failure_cause: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _cause_is_the_reason(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("terminal_failure_cause") is None:
            return {**data, "terminal_failure_cause": data.get("reason")}
        return data
