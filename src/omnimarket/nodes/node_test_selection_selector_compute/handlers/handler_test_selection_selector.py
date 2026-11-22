# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Selector of a narrowed test selection's shard count and weight bound (OMN-23296).

A narrowed selection used to take its shard count from the number of paths. tests/nodes/ with tests/unit/
is four paths and about 4,100 of the suite's recorded seconds, so it ran as two unplanned shards of
about 24,000 tests each (45 to 77 minutes, red on 13 open pull requests as Tests (Split 2/2)). The shard
count now comes from the estimated time: ceil(estimated seconds / target_shard_seconds), never fewer than
the path-count rule. A selection that needs more than max_narrowed_shards takes the planned full-suite
matrix. Durations that cannot be read leave the weight unknown, which also takes the full-suite matrix.
"""

from __future__ import annotations

import math

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_durations_file import (
    SourceDurationsFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_test_files import (
    SourceTestFilesDisk,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_policy_bound import (
    estimate_selected_seconds,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    EnumSelectorOutcome,
    ModelSelectorDecision,
    ModelSelectorPolicy,
    ModelSelectorRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_durations_source import (
    ProtocolDurationsSource,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_test_files_source import (
    ProtocolTestFilesSource,
)


class HandlerTestSelectionSelector:
    """Pure over its injected durations source and policy."""

    def __init__(
        self,
        source: ProtocolDurationsSource | None = None,
        policy: ModelSelectorPolicy | None = None,
        test_files: ProtocolTestFilesSource | None = None,
    ) -> None:
        from omnimarket.handlers.adapter_test_selection_policy_file import load_policy

        self._source = source or SourceDurationsFile()
        self._policy = policy or load_policy()
        self._test_files = test_files or SourceTestFilesDisk()

    def handle(self, request: ModelSelectorRequest) -> ModelSelectorDecision:
        durations = self._source.read(request.repo_root, self._policy.durations_path)
        if durations is None:
            return ModelSelectorDecision(
                outcome=EnumSelectorOutcome.FULL_SUITE_DURATIONS_UNREAD
            )
        listing = self._test_files.list_test_files(request.repo_root)
        if listing is None:
            return ModelSelectorDecision(
                outcome=EnumSelectorOutcome.FULL_SUITE_DURATIONS_UNREAD
            )
        estimated, _untimed = estimate_selected_seconds(
            request.selected_paths,
            durations,
            listing,
            self._policy.untimed_file_seconds,
        )
        by_time = max(1, math.ceil(estimated / self._policy.target_shard_seconds))
        if (
            by_time > self._policy.max_narrowed_shards
            or estimated > self._policy.narrowed_bound_seconds
        ):
            return ModelSelectorDecision(
                outcome=EnumSelectorOutcome.FULL_SUITE_TOO_HEAVY,
                estimated_seconds=estimated,
            )
        shards = max(by_time, self._shards_by_path_count(len(request.selected_paths)))
        return ModelSelectorDecision(
            outcome=EnumSelectorOutcome.NARROWED,
            split_count=shards,
            estimated_seconds=estimated,
            shard_seconds=estimated / shards,
        )

    def _shards_by_path_count(self, paths: int) -> int:
        for up_to, shards in self._policy.path_count_shards:
            if paths <= up_to:
                return shards
        return self._policy.path_count_max_shards
