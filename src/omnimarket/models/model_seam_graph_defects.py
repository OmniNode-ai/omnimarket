# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rules, facts and defect report for the seam graph (OMN-19998).

The graph is built from node contracts plus overlay-supplied rules; it only
reports defects at the seams, carrying no effect of its own.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumSeamDefectClass(StrEnum):
    """Kinds of seam defects the graph can report."""

    TOPIC_SUBSCRIBED_NO_PUBLISHER = "topic_subscribed_no_publisher"
    TOPIC_PUBLISHED_NO_SUBSCRIBER = "topic_published_no_subscriber"
    NODE_NO_CALLER = "node_no_caller"
    NODE_OVERLAY_ROW_MISSING = "node_overlay_row_missing"
    NODE_OVERLAY_KEYS_MISSING = "node_overlay_keys_missing"
    CROSS_REPO_IMPORT_NO_SEAM_TEST = "cross_repo_import_no_seam_test"
    SYNC_IO_HANDLER = "sync_io_handler"
    FILE_LEDGER_READER = "file_ledger_reader"
    SHARED_TOPIC_REPLAY = "shared_topic_replay"


class ModelSeamRepoRule(BaseModel):
    """Per-repo rule naming the repo and where its contracts and callers live."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    package: str
    # repo-relative directory of the package source, scanned for imports
    source_dir: str
    contract_glob: str
    caller_scan_dirs: tuple[str, ...]
    # repo-relative test directories, scanned for seam-test string literals
    test_dirs: tuple[str, ...]


class ModelSeamGraphRule(BaseModel):
    """Overlay-supplied rule driving the whole seam graph scan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic_pattern: str
    command_topic_pattern: str
    first_n: int = Field(ge=1)
    repos: tuple[ModelSeamRepoRule, ...] = Field(min_length=1)
    # comes only from the overlay
    overlay_repo: str
    # e.g. "overlays/{node}/overlay.yaml", relative to the overlay repo root
    overlay_row_glob: str
    scanned_extensions: tuple[str, ...]
    excluded_dir_names: tuple[str, ...]
    # regexes over handler source
    sync_io_markers: tuple[str, ...]
    # regexes over node source
    ledger_reader_markers: tuple[str, ...]
    # substrings of a node name
    replay_node_markers: tuple[str, ...]
    shared_topic_min_consumers: int = Field(ge=2)
    # a file naming more distinct nodes than this is a catalog, never a caller
    catalog_file_node_threshold: int = Field(ge=1)


class ModelSeamGraphRequest(BaseModel):
    """Request pairing the overlay rule with pinned checkouts of each repo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: ModelSeamGraphRule
    # repo name -> absolute path of the pinned checkout
    repo_roots: dict[str, str]


class ModelSeamNodeFact(BaseModel):
    """Everything the scan learned about one node across its seams."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    name: str
    contract_path: str
    node_type: str
    subscribe_topics: tuple[str, ...]
    publish_topics: tuple[str, ...]
    external_consumed_topics: tuple[str, ...]
    external_produced_topics: tuple[str, ...]
    command_topics: tuple[str, ...]
    needs_overlay: bool
    overlay_row_present: bool
    overlay_required_keys: tuple[str, ...]
    overlay_row_keys: tuple[str, ...]
    # "<file>: <marker>" in a sync `def handle`
    sync_io_hits: tuple[str, ...]
    ledger_reader_hits: tuple[str, ...]
    # repo-relative files outside the node's own directory that name it
    referenced_by: tuple[str, ...]


class ModelSeamImportFact(BaseModel):
    """One cross-repo import and whether a seam test covers it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    importer_repo: str
    importer_path: str
    module: str
    owner_repo: str
    has_seam_test: bool


class ModelSeamGraphFacts(BaseModel):
    """Raw facts gathered before defect derivation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[ModelSeamNodeFact, ...]
    imports: tuple[ModelSeamImportFact, ...]
    contracts_read: int
    contracts_unreadable: tuple[str, ...]


class ModelSeamDefect(BaseModel):
    """A single defect pinned to one subject in one repo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    defect_class: EnumSeamDefectClass
    subject: str
    repo: str
    detail: str


class ModelSeamDefectGroup(BaseModel):
    """Defects of one class, grouped with a count and the first few."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    defect_class: EnumSeamDefectClass
    count: int
    first: tuple[ModelSeamDefect, ...]


class ModelSeamGraphCounts(BaseModel):
    """Edge and vertex counts describing the graph that was scanned."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: int
    topics: int
    publish_edges: int
    subscribe_edges: int
    overlay_edges: int
    caller_edges: int
    import_edges: int


class ModelSeamGraphDefectReport(BaseModel):
    """Final report: graph counts plus grouped seam defects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    counts: ModelSeamGraphCounts
    groups: tuple[ModelSeamDefectGroup, ...]
    contracts_read: int
    contracts_unreadable: tuple[str, ...]
    # Set only on a refused scan; the runtime publishes a result carrying a
    # cause on the contract's failure terminal.
    terminal_failure_cause: str | None = None
