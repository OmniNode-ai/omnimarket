# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly attribution: classes, no-evidence cases, determinism and the handler's contract binding."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.test_selector_reading import (
    ATTRIBUTION_LIMITS,
    EnumAttributionClass,
    EnumEvidenceState,
    EnumFileOutcome,
    EnumIncompleteReason,
    EnumSelectorReadingState,
    EnumShardConclusion,
    EnumUnattributableReason,
    ModelAttributionCandidate,
    ModelCandidateRecord,
    ModelCandidateRun,
    ModelCandidateSelection,
    ModelNightCompleteness,
    ModelNightlyAttributionRequest,
)
from omnimarket.nodes.node_test_selector_reading_compute.handlers.handler_nightly_attribution import (
    HandlerNightlyAttribution,
)

pytestmark = pytest.mark.unit

F = "tests/nodes/node_x/test_x.py"
MERGED = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
FULL = ModelCandidateSelection(selected_paths=("tests/",), split_count=2)
NARROW_OTHER = ModelCandidateSelection(
    selected_paths=("tests/nodes/node_y/",), split_count=1
)
NARROW_HIT = ModelCandidateSelection(
    selected_paths=("tests/nodes/node_x/",), split_count=1
)


def _complete(**over: Any) -> ModelNightCompleteness:
    base: dict[str, Any] = {
        "run_terminal": True,
        "shards_expected": 10,
        "artifacts_found": 10,
        "shards_in_error": 0,
        "test_files_reported": 500,
        "min_test_files": 100,
    }
    base.update(over)
    return ModelNightCompleteness(**base)


def _candidate(
    sha: str,
    selection: ModelCandidateSelection,
    *,
    pr: int | None = 1,
    record: bool = True,
    run: bool = True,
    cancelled: bool = False,
    finished: datetime | None = None,
    shards: dict[int, EnumShardConclusion] | None = None,
    run_checkout: str | None = None,
    by_stage: dict[int, ModelCandidateSelection] | None = None,
) -> ModelAttributionCandidate:
    shard_map = (
        shards
        if shards is not None
        else dict.fromkeys(
            range(1, selection.split_count + 1), EnumShardConclusion.SUCCESS
        )
    )
    return ModelAttributionCandidate(
        commit_sha=sha,
        pr_number=pr,
        merged_at=MERGED,
        record=(
            ModelCandidateRecord(
                checkout_sha=f"co-{sha}", executed=selection, by_stage=by_stage or {}
            )
            if record
            else None
        ),
        run=(
            ModelCandidateRun(
                run_id=9,
                attempt=1,
                checkout_sha=run_checkout or f"co-{sha}",
                completed_at=finished or MERGED - timedelta(minutes=5),
                cancelled=cancelled,
                shard_conclusions=shard_map,
            )
            if run
            else None
        ),
    )


def _request(
    candidates: tuple[ModelAttributionCandidate, ...],
    *,
    previous: EnumFileOutcome | None = EnumFileOutcome.PASSED,
    completeness: ModelNightCompleteness | None = None,
    shadow_stage: int | None = None,
) -> ModelNightlyAttributionRequest:
    return ModelNightlyAttributionRequest(
        repo="example-org/example-repo",
        slot_date=date(2026, 11, 21),
        completeness=completeness or _complete(),
        outcomes={
            F: EnumFileOutcome.FAILED,
            "tests/a/test_ok.py": EnumFileOutcome.PASSED,
        },
        previous_outcomes={F: previous} if previous is not None else {},
        candidates=candidates,
        shadow_stage=shadow_stage,
    )


def _attribute(request: ModelNightlyAttributionRequest) -> Any:
    result = HandlerNightlyAttribution().handle(request)
    assert [a.test_file for a in result.attributions] == [F]
    return result.attributions[0]


def test_attribution_classes_miss_names_the_skipping_pull_request() -> None:
    result = _attribute(
        _request(
            (
                _candidate("aaa", FULL, pr=1),
                _candidate("bbb", NARROW_OTHER, pr=2),
                _candidate("ccc", FULL, pr=3),
            )
        )
    )
    assert result.klass is EnumAttributionClass.MISS
    assert result.suspects == ("bbb",)


def test_attribution_classes_interaction_when_every_candidate_selected_and_passed() -> (
    None
):
    result = _attribute(
        _request((_candidate("aaa", FULL), _candidate("bbb", NARROW_HIT, pr=2)))
    )
    assert result.klass is EnumAttributionClass.INTERACTION
    assert result.suspects == ()


def test_attribution_classes_preexisting_when_red_the_night_before() -> None:
    result = _attribute(
        _request((_candidate("aaa", FULL),), previous=EnumFileOutcome.FAILED)
    )
    assert result.klass is EnumAttributionClass.PREEXISTING


def test_attribution_classes_preexisting_with_an_empty_range() -> None:
    result = _attribute(_request((), previous=EnumFileOutcome.ERROR))
    assert result.klass is EnumAttributionClass.PREEXISTING


def test_attribution_classes_unattributable_is_the_fourth_class() -> None:
    result = _attribute(_request((_candidate("aaa", FULL, record=False),)))
    assert result.klass is EnumAttributionClass.UNATTRIBUTABLE
    assert result.reason is EnumUnattributableReason.NO_EVIDENCE


def test_attribution_classes_two_suspects_are_both_named() -> None:
    result = _attribute(
        _request(
            (
                _candidate("bbb", NARROW_OTHER, pr=2),
                _candidate("aaa", NARROW_OTHER, pr=1),
            )
        )
    )
    assert result.klass is EnumAttributionClass.MISS
    assert result.suspects == ("aaa", "bbb")


def test_attribution_classes_one_suspect_beside_a_candidate_without_evidence() -> None:
    result = _attribute(
        _request(
            (
                _candidate("aaa", NARROW_OTHER, pr=1),
                _candidate("bbb", FULL, pr=2, record=False),
            )
        )
    )
    assert result.klass is EnumAttributionClass.MISS
    assert result.suspects == ("aaa",)
    assert [(e.commit_sha, e.state) for e in result.no_evidence] == [
        ("bbb", EnumEvidenceState.MISSING_RECORD)
    ]


def test_attribution_classes_preexisting_wins_over_a_skipper() -> None:
    result = _attribute(
        _request((_candidate("aaa", NARROW_OTHER),), previous=EnumFileOutcome.FAILED)
    )
    assert result.klass is EnumAttributionClass.PREEXISTING
    assert result.suspects == ()


@pytest.mark.parametrize(
    "candidates",
    [
        (_candidate("aaa", NARROW_OTHER, pr=1), _candidate("bbb", FULL, pr=2)),
        (_candidate("aaa", FULL, pr=1), _candidate("bbb", FULL, pr=2, run=False)),
        (_candidate("aaa", FULL, pr=1), _candidate("bbb", NARROW_HIT, pr=2)),
        (_candidate("aaa", NARROW_OTHER, pr=1), _candidate("bbb", NARROW_OTHER, pr=2)),
    ],
)
def test_attribution_classes_do_not_depend_on_pull_request_order(
    candidates: tuple[ModelAttributionCandidate, ...],
) -> None:
    results = [
        _attribute(_request(tuple(order))).model_dump()
        for order in itertools.permutations(candidates)
    ]
    assert results[0] == results[1]


@pytest.mark.parametrize(
    ("kwargs", "state"),
    [
        ({"record": False}, EnumEvidenceState.MISSING_RECORD),
        ({"run": False}, EnumEvidenceState.MISSING_RUN),
        ({"cancelled": True}, EnumEvidenceState.CANCELLED_RUN),
        (
            {
                "shards": {
                    1: EnumShardConclusion.SUCCESS,
                    2: EnumShardConclusion.SKIPPED,
                }
            },
            EnumEvidenceState.SKIPPED_SHARD,
        ),
        (
            {"shards": {1: EnumShardConclusion.SUCCESS}},
            EnumEvidenceState.SKIPPED_SHARD,
        ),
        (
            {"finished": MERGED + timedelta(minutes=1)},
            EnumEvidenceState.FINISHED_AFTER_MERGE,
        ),
        ({"run_checkout": "other"}, EnumEvidenceState.RECORD_MISMATCH),
        (
            {
                "shards": {
                    1: EnumShardConclusion.SUCCESS,
                    2: EnumShardConclusion.FAILURE,
                }
            },
            EnumEvidenceState.SHARD_NOT_SUCCESS,
        ),
    ],
)
def test_no_evidence_is_never_a_pass(
    kwargs: dict[str, Any], state: EnumEvidenceState
) -> None:
    good = _request((_candidate("aaa", FULL),))
    assert _attribute(good).klass is EnumAttributionClass.INTERACTION
    bad = _request((_candidate("aaa", FULL, **kwargs),))
    result = _attribute(bad)
    assert result.klass is EnumAttributionClass.UNATTRIBUTABLE
    assert result.reason is EnumUnattributableReason.NO_EVIDENCE
    assert [e.state for e in result.no_evidence] == [state]


def test_a_new_test_is_unattributable_with_its_reason() -> None:
    for previous in (None, EnumFileOutcome.ABSENT, EnumFileOutcome.SKIPPED):
        result = _attribute(
            _request((_candidate("aaa", NARROW_OTHER),), previous=previous)
        )
        assert result.klass is EnumAttributionClass.UNATTRIBUTABLE
        assert result.reason is EnumUnattributableReason.NO_PRIOR_RESULT


def test_a_direct_push_is_unattributable_with_its_reason() -> None:
    result = _attribute(
        _request(
            (
                _candidate("aaa", FULL),
                _candidate("bbb", FULL, pr=None, record=False, run=False),
            )
        )
    )
    assert result.klass is EnumAttributionClass.UNATTRIBUTABLE
    assert result.reason is EnumUnattributableReason.DIRECT_PUSH


def test_an_empty_commit_range_is_unattributable_with_its_reason() -> None:
    result = _attribute(_request(()))
    assert result.klass is EnumAttributionClass.UNATTRIBUTABLE
    assert result.reason is EnumUnattributableReason.NO_CANDIDATE


def test_shadow_run_reads_the_stage_entry_and_labels_the_result() -> None:
    candidate = _candidate("aaa", FULL, by_stage={1: NARROW_OTHER})
    executed = _attribute(_request((candidate,)))
    shadow = _attribute(_request((candidate,), shadow_stage=1))
    assert executed.klass is EnumAttributionClass.INTERACTION
    assert not executed.shadow
    assert shadow.klass is EnumAttributionClass.MISS
    assert shadow.suspects == ("aaa",)
    assert shadow.shadow


def test_shadow_run_without_the_stage_entry_is_no_evidence() -> None:
    result = _attribute(_request((_candidate("aaa", FULL),), shadow_stage=1))
    assert result.klass is EnumAttributionClass.UNATTRIBUTABLE
    assert [e.state for e in result.no_evidence] == [
        EnumEvidenceState.MISSING_STAGE_ENTRY
    ]


def test_a_miss_reads_the_reading_state_as_not_working() -> None:
    request = _request((_candidate("aaa", NARROW_OTHER),))
    result = HandlerNightlyAttribution().handle(request)
    assert result.reading_state is EnumSelectorReadingState.NOT_WORKING
    assert result.limits == ATTRIBUTION_LIMITS
    assert any("kafka" in limit for limit in result.limits)


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"artifacts_found": 9}, EnumIncompleteReason.MISSING_ARTIFACTS),
        ({"shards_in_error": 1}, EnumIncompleteReason.SHARD_IN_ERROR),
        ({"run_terminal": False}, EnumIncompleteReason.RUN_NOT_TERMINAL),
        ({"test_files_reported": 99}, EnumIncompleteReason.TOO_FEW_TEST_FILES),
    ],
)
def test_an_incomplete_night_attributes_nothing(
    over: dict[str, Any], reason: EnumIncompleteReason
) -> None:
    result = HandlerNightlyAttribution().handle(
        _request((_candidate("aaa", NARROW_OTHER),), completeness=_complete(**over))
    )
    assert not result.complete
    assert result.incomplete_reasons == (reason,)
    assert result.attributions == ()
    assert result.reading_state is EnumSelectorReadingState.UNKNOWN


def test_a_complete_night_with_no_red_file_is_working() -> None:
    request = ModelNightlyAttributionRequest(
        repo="example-org/example-repo",
        slot_date=date(2026, 11, 21),
        completeness=_complete(),
        outcomes={F: EnumFileOutcome.PASSED},
        previous_outcomes={F: EnumFileOutcome.PASSED},
    )
    result = HandlerNightlyAttribution().handle(request)
    assert result.complete
    assert result.attributions == ()
    assert result.reading_state is EnumSelectorReadingState.WORKING


def test_a_commit_listed_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        _request((_candidate("aaa", FULL), _candidate("aaa", FULL)))


def test_the_contract_routes_the_operation_to_the_handler() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_test_selector_reading_compute/contract.yaml"
        ).read_text()
    )
    routes = {
        h["operation"]: h["handler"] for h in contract["handler_routing"]["handlers"]
    }
    assert routes["attribute_nightly_failures"]["name"] == "HandlerNightlyAttribution"
    assert routes["attribute_nightly_failures"]["module"].endswith(
        "handlers.handler_nightly_attribution"
    )
