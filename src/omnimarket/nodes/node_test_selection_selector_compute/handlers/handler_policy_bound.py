# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The narrowed-run bound, shard mapping and full-run crossover, derived from policy and the replay inputs.

One calculation: ``policy.yaml``, the recorded durations file and the replay's per-merge selections go in;
the shard count of each merge, the share of merges over each candidate bound, the test-seconds each bound
saves, the full-run crossover and the per-merge runner-cost columns come out. Nothing is typed in here: the
setup and collection seconds are measurements the caller supplies, and while they are unmeasured the cost
columns and the crossover are left empty rather than guessed.
"""

from __future__ import annotations

import math

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_durations_file import (
    SourceDurationsFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_test_files import (
    SourceTestFilesDisk,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_policy_bound import (
    EnumBoundMergeKind,
    ModelBoundMerge,
    ModelBoundOption,
    ModelMergeCost,
    ModelPolicyBoundRequest,
    ModelPolicyBoundResult,
    PolicyBoundInputError,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_durations_source import (
    ProtocolDurationsSource,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_test_files_source import (
    ProtocolTestFilesSource,
)

_P90 = 0.9


def durations_percentile(durations: dict[str, float], fraction: float) -> float:
    """Nearest-rank percentile of the recorded per-file seconds."""
    if not durations:
        raise ValueError("no recorded durations to take a percentile of")
    ordered = sorted(durations.values())
    return ordered[max(1, math.ceil(fraction * len(ordered))) - 1]


def suite_total_within_tolerance(
    policy: ModelSelectorPolicy, durations: dict[str, float]
) -> bool:
    """Whether ``target_shard_seconds * full_split_count`` matches the recorded total within the declared tolerance."""
    total = sum(durations.values())
    if total <= 0:
        return False
    product = policy.target_shard_seconds * policy.full_split_count
    return abs(product - total) <= policy.record_tolerance_ratio * total


def _matches(test_file: str, path: str) -> bool:
    return test_file == path or (path.endswith("/") and test_file.startswith(path))


def estimate_selected_seconds(
    selected_paths: tuple[str, ...],
    durations: dict[str, float],
    test_files: tuple[str, ...],
    untimed_file_seconds: float,
) -> tuple[float, tuple[str, ...]]:
    """Recorded seconds of the selected files, a file with no entry costing ``untimed_file_seconds``.

    A directory path selects the recorded and the listed test files beneath it; an explicit file path
    selects that file whether it is recorded or not. Returns the seconds and the untimed files by path.
    """
    covered: set[str] = set()
    for path in selected_paths:
        if path.endswith("/"):
            covered.update(f for f in durations if _matches(f, path))
            covered.update(f for f in test_files if _matches(f, path))
        else:
            covered.add(path)
    untimed = tuple(sorted(f for f in covered if f not in durations))
    seconds = sum(durations[f] for f in covered if f in durations)
    return seconds + len(untimed) * untimed_file_seconds, untimed


def narrowed_runner_seconds(
    selected_seconds: float,
    policy: ModelSelectorPolicy,
    setup_seconds: float,
    collection_seconds: float,
) -> float:
    """Runner seconds of a narrowed run: its test seconds, a setup per shard and the collection gate."""
    shards = max(1, math.ceil(selected_seconds / policy.target_shard_seconds))
    return selected_seconds + shards * setup_seconds + collection_seconds


def full_runner_seconds(
    suite_total_seconds: float, policy: ModelSelectorPolicy, setup_seconds: float
) -> float:
    """Runner seconds of the planned full-suite matrix: the recorded total and a setup per shard."""
    return suite_total_seconds + policy.full_split_count * setup_seconds


def crossover_selected_seconds(
    policy: ModelSelectorPolicy,
    suite_total_seconds: float,
    *,
    setup_seconds: float,
    collection_seconds: float,
) -> float | None:
    """Largest selected-seconds figure whose narrowed run still costs less than the full run.

    None when even an empty selection costs the full run's runner seconds or more.
    """
    full = full_runner_seconds(suite_total_seconds, policy, setup_seconds)

    def cheaper(seconds: float) -> bool:
        return (
            narrowed_runner_seconds(seconds, policy, setup_seconds, collection_seconds)
            < full
        )

    if not cheaper(0.0):
        return None
    low, high = 0.0, max(suite_total_seconds, 1.0)
    if cheaper(high):
        return high
    for _ in range(80):
        middle = (low + high) / 2
        if cheaper(middle):
            low = middle
        else:
            high = middle
    return low


class HandlerPolicyBound:
    """Pure over its injected sources and policy; reads nothing but what they answer."""

    def __init__(
        self,
        durations_source: ProtocolDurationsSource | None = None,
        test_files_source: ProtocolTestFilesSource | None = None,
        policy: ModelSelectorPolicy | None = None,
    ) -> None:
        # Imported here: the policy adapter reaches this node's models, whose package imports this module.
        from omnimarket.handlers.adapter_test_selection_policy_file import load_policy

        self._durations = durations_source or SourceDurationsFile()
        self._files = test_files_source or SourceTestFilesDisk()
        self._policy = policy or load_policy()

    def handle(self, request: ModelPolicyBoundRequest) -> ModelPolicyBoundResult:
        policy = self._policy
        durations = self._durations.read(request.repo_root, policy.durations_path)
        if not durations:
            raise PolicyBoundInputError("the durations file could not be read")
        listing = self._files.list_test_files(request.repo_root)
        if listing is None:
            raise PolicyBoundInputError("the test files could not be listed")
        total = sum(durations.values())
        measured = request.shard_setup_seconds is not None
        setup = request.shard_setup_seconds or 0.0
        collection = request.collection_gate_seconds or 0.0

        costs = tuple(
            self._cost(merge, durations, listing, total, setup, collection, measured)
            for merge in request.merges
        )
        narrowed = [c for c in costs if c.kind is EnumBoundMergeKind.NARROWED]
        crossover = (
            crossover_selected_seconds(
                policy, total, setup_seconds=setup, collection_seconds=collection
            )
            if measured
            else None
        )
        bounds: list[tuple[float, str]] = [
            (float(policy.narrowed_bound_seconds), "policy"),
            *[(float(b), "comparison") for b in policy.comparison_bounds_seconds],
        ]
        if crossover is not None:
            bounds.append((crossover, "crossover"))
        seen: set[float] = set()
        options = []
        for bound, source in bounds:
            if bound in seen:
                continue
            seen.add(bound)
            under = [c for c in narrowed if c.selected_seconds <= bound]
            options.append(
                ModelBoundOption(
                    bound_seconds=bound,
                    source=source,
                    merges_over_bound=len(narrowed) - len(under),
                    merges_under_bound=len(under),
                    test_seconds_saved=sum(
                        max(0.0, total - c.selected_seconds) for c in under
                    ),
                )
            )
        more = tuple(
            c.sha
            for c in costs
            if c.shards is not None
            and c.narrowed_runner_seconds is not None
            and c.full_runner_seconds is not None
            and c.narrowed_runner_seconds >= c.full_runner_seconds
        )
        recorded = set(durations)
        return ModelPolicyBoundResult(
            suite_total_seconds=total,
            full_split_count=policy.full_split_count,
            target_times_split_seconds=policy.target_shard_seconds
            * policy.full_split_count,
            suite_total_within_tolerance=suite_total_within_tolerance(
                policy, durations
            ),
            untimed_file_seconds=policy.untimed_file_seconds,
            untimed_file_seconds_p90=durations_percentile(durations, _P90),
            narrowed_bound_seconds=policy.narrowed_bound_seconds,
            narrowed_merge_count=len(narrowed),
            bound_options=tuple(options),
            merge_costs=costs,
            cost_columns_measured=measured,
            shard_cap_seconds=policy.max_narrowed_shards * policy.target_shard_seconds,
            crossover_selected_seconds=crossover,
            merges_costing_more_than_full=more,
            files_without_entry=tuple(sorted(f for f in listing if f not in recorded)),
            entries_without_file=tuple(
                sorted(
                    f
                    for f in recorded
                    if not self._files.file_exists(request.repo_root, f)
                )
            ),
        )

    def _cost(
        self,
        merge: ModelBoundMerge,
        durations: dict[str, float],
        listing: tuple[str, ...],
        total: float,
        setup: float,
        collection: float,
        measured: bool,
    ) -> ModelMergeCost:
        policy = self._policy
        seconds, untimed = estimate_selected_seconds(
            merge.selected_paths, durations, listing, policy.untimed_file_seconds
        )
        narrowed = (
            merge.kind is EnumBoundMergeKind.NARROWED
            and seconds <= policy.narrowed_bound_seconds
        )
        shards = (
            min(
                policy.max_narrowed_shards,
                max(1, math.ceil(seconds / policy.target_shard_seconds)),
            )
            if narrowed
            else None
        )
        return ModelMergeCost(
            sha=merge.sha,
            kind=merge.kind,
            selected_seconds=seconds,
            untimed_file_count=len(untimed),
            shards=shards,
            narrowed_runner_seconds=(
                narrowed_runner_seconds(seconds, policy, setup, collection)
                if narrowed and measured
                else None
            ),
            full_runner_seconds=(
                full_runner_seconds(total, policy, setup)
                if narrowed and measured
                else None
            ),
        )
