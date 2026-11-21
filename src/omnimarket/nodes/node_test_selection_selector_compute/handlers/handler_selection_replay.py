# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replays the test selector over merged history against a frozen tree (stage 0).

Pure over its request: the tree, the policy, the adjacency map and the recorded durations all come from the
request's snapshot, so the same request answers the same on any checkout. The path resolution and the shard
sizing are the selector node's own handlers (``HandlerTestPathResolution`` over the snapshot written out as a
stub tree, and ``HandlerTestSelectionSelector`` over the snapshot's durations and policy). What this module
adds is the rule ladder ``compute_selection`` applies around them, evaluated for every rule so a merge's every
trigger is reported, and the totals. A parity test holds the ladder to ``compute_selection``.
"""

from __future__ import annotations

import re
import tempfile
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_tree_snapshot import (
    materialize_tree,
    read_snapshot_file,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers import (
    handler_test_path_resolution as resolution,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    HandlerTestPathResolution,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    EnumReplayKind,
    ModelInfrastructureCrossTab,
    ModelInfrastructureRow,
    ModelReplayDecision,
    ModelReplayFailed,
    ModelReplayMerge,
    ModelReplayRequest,
    ModelReplayResult,
    ModelReplayTotals,
    ModelReplayTrigger,
    ModelTreeSnapshot,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelPathResolutionRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorRequest,
)

_SECTION = re.compile(r"^\s*\[\[?([^\[\]]+)\]\]?\s*$")

REASON_TEST_INFRASTRUCTURE = "test_infrastructure"
REASON_PROTECTED_SURFACE = "protected_surface"
REASON_UNMAPPED_MODULE = "unmapped_module"
REASON_SHARED_MODULE = "shared_module"
REASON_THRESHOLD_MODULES = "threshold_modules"
REASON_NO_NARROWABLE_SELECTION = "no_narrowable_selection"
REASON_NARROWED_SELECTION_TOO_HEAVY = "narrowed_selection_too_heavy"
REASON_FEATURE_FLAG_OFF = "feature_flag_off"
REASON_MAIN_BRANCH = "main_branch"
REASON_MERGE_GROUP = "merge_group"
REASON_SCHEDULED = "scheduled"


def _sections(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {"": []}
    current = ""
    for line in text.splitlines():
        match = _SECTION.match(line)
        if match:
            current = match.group(1).strip()
            found.setdefault(current, [])
        else:
            found[current].append(line.rstrip())
    return {name: _strip_blank(lines) for name, lines in found.items()}


def _strip_blank(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start]:
        start += 1
    while end > start and not lines[end - 1]:
        end -= 1
    return lines[start:end]


def changed_sections(base_text: str, head_text: str) -> tuple[str, ...]:
    """Headers of the TOML tables whose lines differ between two texts ('' stands for the lines above the first table)."""
    base, head = _sections(base_text), _sections(head_text)
    return tuple(
        sorted(
            name
            for name in set(base) | set(head)
            if base.get(name) != head.get(name)
            and (name or base.get(name) or head.get(name))
        )
    )


class HandlerSelectionReplay:
    """``replay_selection``: stage 0 of the selector over a list of merges."""

    def handle(
        self, request: ModelReplayRequest
    ) -> ModelReplayResult | ModelReplayFailed:
        try:
            snapshot = self._snapshot(request)
        except (OSError, ValueError) as exc:
            return ModelReplayFailed(reason=f"snapshot unreadable: {exc}")
        missing = [
            entry
            for entry in snapshot.adjacency.test_infrastructure_paths
            if entry not in request.infrastructure_categories
        ]
        if missing:
            return ModelReplayFailed(
                reason=f"infrastructure_categories lacks entries {missing}"
            )

        selector = HandlerTestSelectionSelector(
            SourceInMemory(snapshot.durations), snapshot.policy
        )
        suite_seconds = round(sum(snapshot.durations.values()), 6)
        try:
            with tempfile.TemporaryDirectory(prefix="selection-replay-") as scratch:
                root = Path(scratch)
                materialize_tree(snapshot, root)
                decisions = tuple(
                    self._decide(
                        merge, request, snapshot, root, selector, suite_seconds
                    )
                    for merge in request.merges
                )
        finally:
            resolution.clear_import_graph_cache()

        return ModelReplayResult(
            stage=request.stage,
            tree_id=snapshot.tree_id,
            snapshot_sha256=request.snapshot_sha256,
            suite_seconds=suite_seconds,
            decisions=decisions,
            infrastructure_cross_tab=self._cross_tab(request, snapshot, decisions),
            totals=self._totals(decisions, suite_seconds),
        )

    @staticmethod
    def _snapshot(request: ModelReplayRequest) -> ModelTreeSnapshot:
        if request.snapshot is not None:
            return request.snapshot
        if request.snapshot_path is None or request.snapshot_sha256 is None:
            raise ValueError("the request names no snapshot")
        snapshot = read_snapshot_file(
            request.repo_root, request.snapshot_path, request.snapshot_sha256
        )
        if snapshot.tree_id != request.tree_id:
            raise ValueError(
                f"snapshot is tree {snapshot.tree_id}, the request names {request.tree_id}"
            )
        return snapshot

    def _decide(
        self,
        merge: ModelReplayMerge,
        request: ModelReplayRequest,
        snapshot: ModelTreeSnapshot,
        root: Path,
        selector: HandlerTestSelectionSelector,
        suite_seconds: float,
    ) -> ModelReplayDecision:
        config = snapshot.adjacency
        changed = list(merge.changed_files)
        triggers: list[ModelReplayTrigger] = []

        def hit(reason: str, evidence: str, entry: str | None = None) -> None:
            triggers.append(
                ModelReplayTrigger(reason=reason, evidence=evidence, entry=entry)
            )

        if not request.flag_enabled:
            hit(REASON_FEATURE_FLAG_OFF, "flag")
        if request.ref_name == "main":
            hit(REASON_MAIN_BRANCH, request.ref_name)
        if request.event_name == "merge_group":
            hit(REASON_MERGE_GROUP, request.event_name)
        if request.event_name == "schedule":
            hit(REASON_SCHEDULED, request.event_name)

        for path in changed:
            for infra in config.test_infrastructure_paths:
                if path == infra or path.startswith(infra.rstrip("/") + "/"):
                    hit(REASON_TEST_INFRASTRUCTURE, path, infra)
                    break
        for path in changed:
            for pattern in config.full_suite_path_globs:
                if fnmatchcase(path, pattern):
                    hit(REASON_PROTECTED_SURFACE, path, pattern)
                    break
        source_paths = [
            path[len(resolution.SRC_PREFIX) :]
            for path in changed
            if path.startswith(resolution.SRC_PREFIX)
        ]
        for path in source_paths:
            if "/" not in path or path.split("/", 1)[0] not in config.adjacency:
                hit(REASON_UNMAPPED_MODULE, resolution.SRC_PREFIX + path)
        changed_modules = sorted({path.split("/", 1)[0] for path in source_paths})
        for module in changed_modules:
            if module in config.shared_modules:
                hit(REASON_SHARED_MODULE, module)
        if len(changed_modules) >= config.thresholds.modules_changed_for_full_suite:
            hit(REASON_THRESHOLD_MODULES, ",".join(changed_modules))

        if triggers:
            return self._full(
                merge, triggers, request.full_split_count, suite_seconds, None
            )

        resolved = HandlerTestPathResolution().handle(
            ModelPathResolutionRequest(
                changed_files=tuple(changed),
                adjacency=config,
                repo_root=str(root),
            )
        )
        selected = list(resolved.selected_paths)
        if not selected and not resolved.data_only:
            hit(REASON_NO_NARROWABLE_SELECTION, "selection")
            return self._full(
                merge, triggers, request.full_split_count, suite_seconds, None
            )
        if not any(path == resolution.TEST_PREFIX for path in selected):
            selected = sorted(set(selected) | config.narrowed_extra_paths(changed))
        sized = selector.handle(
            ModelSelectorRequest(selected_paths=tuple(selected), repo_root=str(root))
        )
        if sized.split_count is None:
            hit(REASON_NARROWED_SELECTION_TOO_HEAVY, "selection")
            return self._full(
                merge,
                triggers,
                request.full_split_count,
                suite_seconds,
                sized.estimated_seconds,
            )
        return ModelReplayDecision(
            sha=merge.sha,
            changed_files=merge.changed_files,
            kind=EnumReplayKind.NARROWED,
            reason_code="narrowed",
            primary_reason=None,
            triggers=(),
            selected_paths=tuple(selected),
            split_count=sized.split_count,
            selected_seconds=sized.estimated_seconds or 0.0,
        )

    @staticmethod
    def _full(
        merge: ModelReplayMerge,
        triggers: list[ModelReplayTrigger],
        split_count: int,
        suite_seconds: float,
        narrowed_estimate: float | None,
    ) -> ModelReplayDecision:
        reason = triggers[0].reason
        return ModelReplayDecision(
            sha=merge.sha,
            changed_files=merge.changed_files,
            kind=EnumReplayKind.FULL_SUITE,
            reason_code=reason,
            primary_reason=reason,
            triggers=tuple(triggers),
            selected_paths=(resolution.TEST_PREFIX,),
            split_count=split_count,
            selected_seconds=suite_seconds,
            narrowed_estimate_seconds=narrowed_estimate,
        )

    @staticmethod
    def _cross_tab(
        request: ModelReplayRequest,
        snapshot: ModelTreeSnapshot,
        decisions: tuple[ModelReplayDecision, ...],
    ) -> ModelInfrastructureCrossTab:
        order = {
            entry: index
            for index, entry in enumerate(snapshot.adjacency.test_infrastructure_paths)
        }
        merges = {merge.sha: merge for merge in request.merges}
        rows: list[ModelInfrastructureRow] = []
        for decision in decisions:
            if decision.primary_reason != REASON_TEST_INFRASTRUCTURE:
                continue
            entries = sorted(
                {
                    t.entry
                    for t in decision.triggers
                    if t.reason == REASON_TEST_INFRASTRUCTURE and t.entry
                },
                key=lambda entry: order[entry],
            )
            categories = sorted({request.infrastructure_categories[e] for e in entries})
            sections: dict[str, tuple[str, ...]] = {}
            for data in merges[decision.sha].data_files:
                if data.path in request.data_file_paths:
                    sections[data.path] = changed_sections(
                        data.base_text, data.head_text
                    )
            rows.append(
                ModelInfrastructureRow(
                    sha=decision.sha,
                    entries=tuple(entries),
                    categories=tuple(categories),
                    cell="+".join(categories),
                    changed_sections=sections,
                )
            )
        cells = Counter(row.cell for row in rows)
        return ModelInfrastructureCrossTab(
            merges=len(rows), rows=tuple(rows), cells=dict(sorted(cells.items()))
        )

    @staticmethod
    def _totals(
        decisions: tuple[ModelReplayDecision, ...], suite_seconds: float
    ) -> ModelReplayTotals:
        reasons = Counter(d.primary_reason for d in decisions if d.primary_reason)
        return ModelReplayTotals(
            merges=len(decisions),
            narrowed=sum(d.kind is EnumReplayKind.NARROWED for d in decisions),
            full_suite=sum(d.kind is EnumReplayKind.FULL_SUITE for d in decisions),
            reason_counts=dict(sorted(reasons.items())),
            selected_seconds=round(sum(d.selected_seconds for d in decisions), 6),
            full_suite_seconds=round(suite_seconds * len(decisions), 6),
            selected_shards=sum(d.split_count for d in decisions),
        )
