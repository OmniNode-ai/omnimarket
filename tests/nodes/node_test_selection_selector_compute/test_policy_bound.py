# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The narrowed-run bound, the shard mapping and the untimed-file cost come from policy and the durations file.

Every test here carries ``policy_bound`` in its name, so ``-k policy_bound`` runs them all.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_durations_file import (
    SourceDurationsFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_test_files import (
    SourceTestFilesInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_policy_bound import (
    HandlerPolicyBound,
    crossover_selected_seconds,
    durations_percentile,
    suite_total_within_tolerance,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_policy_bound import (
    EnumBoundMergeKind,
    ModelBoundMerge,
    ModelPolicyBoundRequest,
    ModelPolicyBoundResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelTreeSnapshot,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    EnumSelectorOutcome,
    ModelSelectorDecision,
    ModelSelectorPolicy,
    ModelSelectorRequest,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY = load_policy()


def _durations() -> dict[str, float]:
    return SourceDurationsFile().read(str(REPO_ROOT), POLICY.durations_path) or {}


def _policy(**changes: float | int) -> ModelSelectorPolicy:
    return POLICY.model_copy(update=changes)


# AC1: the policy test reads the durations file.


def test_policy_bound_target_times_split_count_matches_the_recorded_suite_total() -> (
    None
):
    durations = _durations()
    assert durations, "the durations file must be readable"
    total = sum(durations.values())
    product = POLICY.target_shard_seconds * POLICY.full_split_count
    tolerance = POLICY.record_tolerance_ratio * total
    assert abs(product - total) <= tolerance, (
        f"target_shard_seconds * full_split_count = {product} but the durations file records {total:.1f}"
    )


def test_policy_bound_tolerance_check_fails_when_the_total_drifts() -> None:
    durations = _durations()
    assert suite_total_within_tolerance(POLICY, durations) is True
    doubled = {name: seconds * 2 for name, seconds in durations.items()}
    assert suite_total_within_tolerance(POLICY, doubled) is False
    assert suite_total_within_tolerance(POLICY, {}) is False


def test_policy_bound_untimed_file_seconds_is_the_durations_p90() -> None:
    durations = _durations()
    p90 = durations_percentile(durations, 0.9)
    assert abs(POLICY.untimed_file_seconds - p90) <= POLICY.record_tolerance_ratio * p90
    assert durations_percentile({"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}, 0.9) == 4.0
    with pytest.raises(ValueError, match="no recorded durations"):
        durations_percentile({}, 0.9)


def test_policy_bound_the_provisional_bound_is_1800_seconds() -> None:
    assert POLICY.narrowed_bound_seconds == 1800
    assert POLICY.narrowed_bound_seconds <= (
        POLICY.max_narrowed_shards * POLICY.target_shard_seconds
    )
    assert 1800 in POLICY.comparison_bounds_seconds
    assert 5400 in POLICY.comparison_bounds_seconds


def test_policy_bound_frozen_snapshot_policy_without_the_new_fields_keeps_its_old_behaviour() -> (
    None
):
    legacy = {
        "target_shard_seconds": 360,
        "max_narrowed_shards": 5,
        "durations_path": "config/test_file_durations.json",
        "path_count_shards": [[2, 1], [5, 2]],
        "path_count_max_shards": 5,
        "selection_stage": 0,
        "full_split_count": 20,
        "record_retention_days": 14,
    }
    # the live policy still requires every field ...
    with pytest.raises(ValidationError):
        ModelSelectorPolicy.model_validate(legacy)
    # ... a frozen snapshot's policy, taken before they existed, reads as the policy it was: the bound
    # is the shard cap's reach (1,800 s), an untimed file costs nothing, no comparison bound is priced
    upgraded = ModelSelectorPolicy.model_validate(
        ModelTreeSnapshot._legacy_policy_fields(legacy)
    )
    assert upgraded.narrowed_bound_seconds == 5 * 360
    assert upgraded.untimed_file_seconds == 0
    assert upgraded.comparison_bounds_seconds == ()
    declared = {**legacy, "narrowed_bound_seconds": 900}
    assert ModelTreeSnapshot._legacy_policy_fields(declared) is declared


def test_policy_bound_model_refuses_a_bound_the_shard_cap_cannot_carry() -> None:
    with pytest.raises(ValidationError):
        ModelSelectorPolicy.model_validate(
            {
                **POLICY.model_dump(),
                "narrowed_bound_seconds": POLICY.target_shard_seconds * 99,
            }
        )
    with pytest.raises(ValidationError):
        ModelSelectorPolicy.model_validate(
            {**POLICY.model_dump(), "narrowed_bound_seconds": 10**9}
        )
    with pytest.raises(ValidationError):
        ModelSelectorPolicy.model_validate(
            {**POLICY.model_dump(), "untimed_file_seconds": -1}
        )


# The shard mapping: ceil(selected_seconds / target_shard_seconds), capped, with the bound.

SMALL = _policy(
    target_shard_seconds=100,
    max_narrowed_shards=5,
    narrowed_bound_seconds=450,
    untimed_file_seconds=7,
    path_count_shards=((2, 1), (5, 2)),
    path_count_max_shards=3,
)
FIXTURE = {
    "tests/a/test_x.py": 250.0,
    "tests/a/test_y.py": 50.0,
    "tests/b/test_z.py": 1000.0,
}


def _selector(
    durations: dict[str, float] | None,
    listing: tuple[str, ...] = (),
    policy: ModelSelectorPolicy = SMALL,
) -> HandlerTestSelectionSelector:
    return HandlerTestSelectionSelector(
        SourceInMemory(durations), policy, SourceTestFilesInMemory(listing)
    )


def _select(
    handler: HandlerTestSelectionSelector, *paths: str
) -> ModelSelectorDecision:
    return handler.handle(ModelSelectorRequest(selected_paths=paths, repo_root="/x"))


def test_policy_bound_shard_count_is_ceil_of_seconds_over_target() -> None:
    decision = _select(_selector(FIXTURE), "tests/a/")
    assert decision.outcome is EnumSelectorOutcome.NARROWED
    assert decision.estimated_seconds == 300.0
    assert decision.split_count == math.ceil(300.0 / SMALL.target_shard_seconds)


def test_policy_bound_over_the_bound_takes_the_full_suite_even_under_the_shard_cap() -> (
    None
):
    durations = {"tests/a/test_x.py": 460.0}
    decision = _select(_selector(durations), "tests/a/")
    # 460 s is 5 shards (within max_narrowed_shards = 5) but over the 450 s bound.
    assert decision.outcome is EnumSelectorOutcome.FULL_SUITE_TOO_HEAVY
    at_bound = _select(_selector({"tests/a/test_x.py": 450.0}), "tests/a/")
    assert at_bound.outcome is EnumSelectorOutcome.NARROWED


def test_policy_bound_shard_count_is_capped_at_the_maximum() -> None:
    wide = SMALL.model_copy(
        update={"narrowed_bound_seconds": 500, "max_narrowed_shards": 5}
    )
    decision = _select(_selector({"tests/a/test_x.py": 500.0}, policy=wide), "tests/a/")
    assert decision.split_count == 5


# AC3: a file with no duration entry costs untimed_file_seconds.


def test_policy_bound_a_deleted_entry_costs_untimed_file_seconds() -> None:
    before = _select(_selector(FIXTURE, tuple(FIXTURE)), "tests/a/")
    trimmed = {k: v for k, v in FIXTURE.items() if k != "tests/a/test_y.py"}
    after = _select(_selector(trimmed, tuple(FIXTURE)), "tests/a/")
    assert before.estimated_seconds == 300.0
    # The file is still on disk, its entry is gone: it now costs untimed_file_seconds, no more, no less.
    assert after.estimated_seconds == 250.0 + SMALL.untimed_file_seconds


def test_policy_bound_a_listed_file_with_no_entry_adds_exactly_untimed_seconds() -> (
    None
):
    base = _select(_selector(FIXTURE, tuple(FIXTURE)), "tests/a/")
    extra = _select(_selector(FIXTURE, (*FIXTURE, "tests/a/test_new.py")), "tests/a/")
    assert extra.estimated_seconds is not None
    assert base.estimated_seconds is not None
    assert (
        extra.estimated_seconds - base.estimated_seconds == SMALL.untimed_file_seconds
    )


def test_policy_bound_an_explicit_file_with_no_entry_is_untimed_not_free() -> None:
    decision = _select(_selector(FIXTURE), "tests/a/test_brand_new.py")
    assert decision.estimated_seconds == SMALL.untimed_file_seconds


def test_policy_bound_a_directory_with_no_files_costs_nothing() -> None:
    decision = _select(_selector(FIXTURE), "tests/none/")
    assert decision.estimated_seconds == 0.0


# The runnable calculation.

MERGES = (
    ModelBoundMerge(
        sha="a" * 40,
        selected_paths=("tests/a/test_y.py",),
        kind=EnumBoundMergeKind.NARROWED,
    ),
    ModelBoundMerge(
        sha="b" * 40,
        selected_paths=("tests/a/",),
        kind=EnumBoundMergeKind.NARROWED,
    ),
    ModelBoundMerge(
        sha="c" * 40,
        selected_paths=("tests/b/",),
        kind=EnumBoundMergeKind.NARROWED,
    ),
    ModelBoundMerge(
        sha="d" * 40,
        selected_paths=("tests/",),
        kind=EnumBoundMergeKind.FULL_SUITE,
    ),
)
BOUND_POLICY = SMALL.model_copy(
    update={
        "full_split_count": 4,
        "max_narrowed_shards": 10,
        "narrowed_bound_seconds": 1000,
        "comparison_bounds_seconds": (300, 450),
    }
)


def _calc(
    setup: float | None = 5.0,
    collection: float | None = 10.0,
    durations: dict[str, float] | None = FIXTURE,
    listing: tuple[str, ...] | None = None,
    merges: tuple[ModelBoundMerge, ...] = MERGES,
) -> ModelPolicyBoundResult:
    handler = HandlerPolicyBound(
        SourceInMemory(durations),
        SourceTestFilesInMemory(tuple(FIXTURE) if listing is None else listing),
        BOUND_POLICY,
    )
    return handler.handle(
        ModelPolicyBoundRequest(
            repo_root="/x",
            merges=merges,
            shard_setup_seconds=setup,
            collection_gate_seconds=collection,
        )
    )


def test_policy_bound_calculation_derives_counts_at_every_bound() -> None:
    result = _calc()
    by_bound = {option.bound_seconds: option for option in result.bound_options}
    assert set(by_bound) >= {300.0, 450.0, 1000.0}
    # Narrowed merges cost 50, 300 and 1000 seconds.
    assert by_bound[300.0].merges_over_bound == 1
    assert by_bound[300.0].merges_under_bound == 2
    assert by_bound[1000.0].merges_over_bound == 0
    total = sum(FIXTURE.values())
    assert by_bound[300.0].test_seconds_saved == (total - 50) + (total - 300)
    assert by_bound[1000.0].test_seconds_saved == (
        (total - 50) + (total - 300) + (total - 1000)
    )
    assert result.narrowed_merge_count == 3


def test_policy_bound_the_recorded_total_and_the_tolerance_are_reported() -> None:
    result = _calc()
    assert result.suite_total_seconds == sum(FIXTURE.values())
    assert result.full_split_count == 4
    assert result.target_times_split_seconds == 400
    assert result.untimed_file_seconds == 7
    assert result.suite_total_within_tolerance is False


def test_policy_bound_crossover_is_where_narrowed_stops_costing_less() -> None:
    total = sum(FIXTURE.values())
    setup, collection = 5.0, 10.0
    crossover = crossover_selected_seconds(
        BOUND_POLICY, total, setup_seconds=setup, collection_seconds=collection
    )
    assert crossover is not None

    def narrowed(seconds: float) -> float:
        shards = max(1, math.ceil(seconds / BOUND_POLICY.target_shard_seconds))
        return seconds + shards * setup + collection

    full = total + BOUND_POLICY.full_split_count * setup
    assert narrowed(crossover) < full
    assert narrowed(crossover + 1) >= full
    assert _calc(setup, collection).crossover_selected_seconds == crossover


def test_policy_bound_the_shard_cap_is_reported_beside_the_crossover() -> None:
    result = _calc()
    assert result.shard_cap_seconds == (
        BOUND_POLICY.max_narrowed_shards * BOUND_POLICY.target_shard_seconds
    )


def test_policy_bound_crossover_is_unknown_until_setup_is_measured() -> None:
    result = _calc(setup=None, collection=None)
    assert result.crossover_selected_seconds is None
    assert all(row.narrowed_runner_seconds is None for row in result.merge_costs)
    assert result.merges_costing_more_than_full == ()
    assert result.cost_columns_measured is False


# AC4: the per-merge cost columns.


def test_policy_bound_every_narrowed_merge_costs_less_than_the_full_run() -> None:
    result = _calc(setup=5.0, collection=10.0)
    assert result.cost_columns_measured is True
    narrowed = [r for r in result.merge_costs if r.shards is not None]
    assert len(narrowed) == 3
    for row in narrowed:
        assert row.narrowed_runner_seconds is not None
        assert row.full_runner_seconds is not None
        assert row.narrowed_runner_seconds < row.full_runner_seconds
    assert result.merges_costing_more_than_full == ()


def test_policy_bound_a_narrowed_merge_costing_more_than_full_is_named() -> None:
    result = _calc(setup=500.0, collection=10.0)
    assert result.cost_columns_measured is True
    # The 1000 s merge needs 10 shards against the full plan's 4: its setup alone outweighs the saving.
    assert result.merges_costing_more_than_full == ("c" * 40,)


def test_policy_bound_a_full_suite_merge_has_no_narrowed_cost() -> None:
    result = _calc()
    full_rows = [r for r in result.merge_costs if r.sha == "d" * 40]
    assert full_rows[0].shards is None
    assert full_rows[0].narrowed_runner_seconds is None


# The listing by path.


def test_policy_bound_listing_names_every_file_without_an_entry_and_entry_without_a_file() -> (
    None
):
    durations = {**FIXTURE, "tests/gone/test_old.py": 3.0}
    listing = (*FIXTURE, "tests/a/test_new.py", "tests/c/test_new2.py")
    result = _calc(durations=durations, listing=listing)
    assert result.files_without_entry == (
        "tests/a/test_new.py",
        "tests/c/test_new2.py",
    )
    assert result.entries_without_file == ("tests/gone/test_old.py",)


# Fail closed.


def test_policy_bound_unreadable_durations_raise_a_typed_error() -> None:
    from omnimarket.nodes.node_test_selection_selector_compute.models.model_policy_bound import (
        PolicyBoundInputError,
    )

    with pytest.raises(PolicyBoundInputError):
        _calc(durations=None)
    with pytest.raises(PolicyBoundInputError):
        HandlerPolicyBound(
            SourceInMemory(FIXTURE), SourceTestFilesInMemory(None), BOUND_POLICY
        ).handle(
            ModelPolicyBoundRequest(
                repo_root="/x",
                merges=MERGES,
                shard_setup_seconds=1.0,
                collection_gate_seconds=0.0,
            )
        )


def test_policy_bound_request_models_are_frozen_and_forbid_extras() -> None:
    with pytest.raises(ValidationError):
        ModelBoundMerge.model_validate(
            {
                "sha": "a",
                "selected_paths": ("t",),
                "kind": EnumBoundMergeKind.NARROWED,
                "extra_field": 1,
            }
        )
    with pytest.raises(ValidationError):
        ModelPolicyBoundRequest(repo_root="/x", merges=())
    with pytest.raises(ValidationError):
        ModelPolicyBoundRequest(repo_root="/x", merges=MERGES, shard_setup_seconds=-1.0)
    merge = MERGES[0]
    frozen_field = "sha"
    with pytest.raises(ValidationError):
        setattr(merge, frozen_field, "z")


def test_policy_bound_live_files_listing_runs_on_the_real_checkout() -> None:
    from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_test_files import (
        SourceTestFilesDisk,
    )

    listing = SourceTestFilesDisk().list_test_files(str(REPO_ROOT))
    assert listing is not None
    assert len(listing) > 1000
    assert all(name.startswith("tests/") for name in listing)
    assert SourceTestFilesDisk().file_exists(str(REPO_ROOT), "tests/conftest.py")
    assert not SourceTestFilesDisk().file_exists(str(REPO_ROOT), "tests/nope_zz.py")
    json.dumps(listing[:1])
