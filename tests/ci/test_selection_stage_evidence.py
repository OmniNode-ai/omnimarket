# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stage gate: a stage of test selection is entered one at a time, on one comparison recomputed from the recorded set.

Reads the real ``policy.yaml``, the receipts under ``config/test_selection_stage_receipts/``, the recorded set the
baseline replay receipt pins and the repository's CODEOWNERS file, and runs the selector node's
``validate_stage_receipt`` operation over them. A receipt records one comparison (the selection the selector gives for
each recorded change against the failures the full run of that change recorded); the validator recomputes every figure
and refuses a receipt that differs, a comparison with a miss, a stage skipped and a receipt with no measured target.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.adapter_test_selection_policy_file import (
    POLICY_PATH,
    load_policy,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_durations_file import (
    SourceDurationsFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_recorded_set_file import (
    SourceRecordedSetFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_recorded_set_in_memory import (
    SourceRecordedSetInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_record import (
    HandlerSelectionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_replay import (
    HandlerSelectionReplay,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_receipt_validation import (
    HandlerStageReceiptValidation,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_recorded_set import (
    ModelRecordedSet,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayRequest,
    ModelReplayResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_comparison import (
    ModelRecordedFullRun,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    EnumStageReceiptViolation as Code,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    ModelStageReceipt,
    ModelStageReceiptValidationRequest,
    ModelStageReceiptValidationResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    ModelSelectionRecordRequest,
    ModelTestSelection,
)
from scripts.ci.detect_test_paths import compute_selection

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPTS_DIR = REPO_ROOT / "config" / "test_selection_stage_receipts"
BASELINE_RECEIPT = REPO_ROOT / "config" / "test_selection_replay_receipt.json"
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"
ADJACENCY = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
POLICY_REL = POLICY_PATH.relative_to(REPO_ROOT).as_posix()
GATE_PATHS = (
    POLICY_REL,
    "config/test_selection_stage_receipts/",
    "tests/ci/test_selection_stage_evidence.py",
)
OWNER_TEAM = "@OmniNode-ai/platform-leads"


def _stage_one() -> dict[str, Any]:
    raw = yaml.safe_load((RECEIPTS_DIR / "stage_1.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _receipt(**changes: Any) -> ModelStageReceipt:
    return ModelStageReceipt.model_validate({**_stage_one(), **changes})


def _baseline() -> ModelStageReceipt:
    return ModelStageReceipt(stage=0)


def _recorded() -> ModelRecordedSet:
    """The recorded set the stage 1 receipt names, read from this tree."""
    recorded = SourceRecordedSetFile(REPO_ROOT).read_recorded_set(
        "config/test_selection_replay_input.json",
        "config/test_selection_observed_input.json",
    )
    assert recorded is not None
    return recorded


def _with_runs(*runs: ModelRecordedFullRun) -> ModelRecordedSet:
    """The recorded set with the given runs in place of the recorded run of the same change."""
    recorded = _recorded()
    originals = {run.sha: run for run in recorded.runs}
    replaced = {run.sha for run in runs}
    kept = tuple(run for run in recorded.runs if run.sha not in replaced)
    swapped = tuple(
        run.model_copy(update={"wall_minutes": originals[run.sha].wall_minutes})
        for run in runs
    )
    return recorded.model_copy(update={"runs": (*kept, *swapped)})


class _ReplayOnce(HandlerSelectionReplay):
    """The replay handler, answering a request it has already replayed from memory (the replay is pure)."""

    _answers: dict[str, Any] = {}

    def handle(self, request: ModelReplayRequest) -> Any:
        key = request.model_dump_json()
        if key not in self._answers:
            self._answers[key] = super().handle(request)
        return self._answers[key]


def _validate(
    *receipts: ModelStageReceipt,
    current: int,
    base: int | None,
    recorded: ModelRecordedSet | None,
) -> ModelStageReceiptValidationResult:
    handler = HandlerStageReceiptValidation(
        SourceRecordedSetInMemory(recorded), load_policy(), replay=_ReplayOnce()
    )
    return handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=current, base_stage=base, receipts=tuple(receipts)
        )
    )


def _codes(result: ModelStageReceiptValidationResult) -> set[Code]:
    return {violation.code for violation in result.violations}


def _decision_runs() -> tuple[str, str, str]:
    """A narrowed change's sha (one whose recorded run is readable), a path its selection holds and one it does not."""
    recorded = _recorded()
    readable = {run.sha for run in recorded.runs if run.readable}
    replayed = _ReplayOnce().handle(recorded.replay_input)
    assert isinstance(replayed, ModelReplayResult)
    for decision in replayed.decisions:
        if decision.kind == "narrowed" and decision.sha in readable:
            held = decision.selected_paths[0].rstrip("/")
            return decision.sha, held, "tests/test_a_file_no_selection_holds.py"
    raise AssertionError(
        "the recorded set holds no narrowed change with a readable run"
    )


def _tree_receipts() -> tuple[ModelStageReceipt, ...]:
    return tuple(
        ModelStageReceipt.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        for path in sorted(RECEIPTS_DIR.glob("stage_*.yaml"))
    )


_GIT_LOCATION_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def scrub_git_location_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop the git location variables before shelling out to git (OMN-14891)."""
    scrubbed = {k: v for k, v in env.items() if k not in _GIT_LOCATION_ENV}
    scrubbed["GIT_CONFIG_NOSYSTEM"] = "1"
    return scrubbed


def _base_stage() -> int | None:
    """The stage on the base branch, read from git when the base ref is present; None when it is not."""
    for ref in ("origin/dev", "origin/main"):
        shown = subprocess.run(
            ["git", "show", f"{ref}:{POLICY_REL}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            env=scrub_git_location_env(os.environ),
        )
        if shown.returncode == 0:
            # A base older than the stage gate has no key, which is stage 0.
            return int(yaml.safe_load(shown.stdout).get("selection_stage", 0))
    return None


# -- the tree as it stands ----------------------------------------------------


def test_selection_stage_of_this_tree_has_the_receipts_it_needs() -> None:
    """The real policy, the real receipts, and the recorded set read from this tree."""
    stage = load_policy().selection_stage
    handler = HandlerStageReceiptValidation(
        SourceRecordedSetFile(REPO_ROOT), load_policy(), replay=_ReplayOnce()
    )
    result = handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=stage, base_stage=_base_stage(), receipts=_tree_receipts()
        )
    )
    assert result.valid, [v.model_dump() for v in result.violations]


def test_selection_stage_policy_holds_the_gate_settings() -> None:
    policy = load_policy()
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert raw["stage_min_compared_changes"] == policy.stage_min_compared_changes
    assert policy.stage_min_compared_changes >= 1
    assert "stage_clean_nights" not in raw
    assert "stage_min_pr_records" not in raw
    comments = POLICY_PATH.read_text(encoding="utf-8")
    assert "#   stage_min_compared_changes\n" in comments
    assert "one passing" in comments


def test_selection_stage_this_tree_is_at_stage_one_on_stage_zero_and_one_receipts() -> (
    None
):
    receipts = _tree_receipts()
    assert load_policy().selection_stage == 1
    assert [r.stage for r in receipts] == [0, 1]
    assert receipts[0] == _baseline()


def test_selection_stage_receipt_reads_the_recorded_set_the_baseline_receipt_pins() -> (
    None
):
    """The inputs of the comparison are the replay inputs the T1 baseline receipt pins, by path and digest."""
    pinned = json.loads(BASELINE_RECEIPT.read_text(encoding="utf-8"))
    comparison = _receipt().comparison
    assert comparison is not None
    assert comparison.replay_input_path == pinned["replay_input_path"]
    assert comparison.replay_input_sha256 == pinned["replay_input_sha256"]
    assert comparison.observed_input_path == pinned["observed_input_path"]
    assert comparison.observed_input_sha256 == pinned["observed_input_sha256"]


# -- AC2: at stage 0 a replay's decisions equal today's ----------------------------


@pytest.mark.parametrize(
    ("changed", "ref"),
    [
        (["pyproject.toml"], "feature/branch"),
        (["src/omnimarket/models/x.py"], "feature/branch"),
        (["contracts/OMN-1.yaml"], "feature/branch"),
        (["src/omnimarket/intelligence/client.py"], "main"),
    ],
)
def test_selection_stage_zero_replay_equals_todays_decisions(
    changed: list[str], ref: str
) -> None:
    """The record built at stage 0 runs exactly what today's selector decides, from the checked-in durations."""
    policy = load_policy().model_copy(update={"selection_stage": 0})
    decided = compute_selection(
        changed_files=changed,
        adjacency_path=ADJACENCY,
        ref_name=ref,
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    selector = HandlerTestSelectionSelector(SourceDurationsFile(), policy)
    record = HandlerSelectionRecord(selector, policy).handle(
        ModelSelectionRecordRequest(
            repo="OmniNode-ai/omnimarket",
            pr_number=7,
            head_sha="a" * 40,
            base_sha="b" * 40,
            checkout_sha="c" * 40,
            run_id=1,
            run_attempt=1,
            changed_files=tuple(changed),
            flag_enabled=True,
            stage_selections={0: decided},
            repo_root=str(REPO_ROOT),
            selector_version="1.0.0",
            policy_version="p" * 12,
        )
    )
    executed = record.executed_selection
    assert record.selection_stage == 0
    assert executed == record.selection_by_stage[0]
    assert executed.selected_paths == decided.selected_paths
    assert executed.split_count == decided.split_count
    assert (executed.kind == "full_suite") is decided.is_full_suite
    assert executed.reason_code == decided.full_suite_reason


def test_selection_stage_one_executes_the_selection_stage_zero_decides() -> None:
    """At the policy's stage 1 the record runs the entry of stage 1, which holds what stage 0 decides."""
    policy = load_policy()
    assert policy.selection_stage == 1
    changed = ["contracts/OMN-1.yaml"]
    decided = compute_selection(
        changed_files=changed,
        adjacency_path=ADJACENCY,
        ref_name="feature/branch",
        event_name="pull_request",
        feature_flag_enabled=True,
    )
    selector = HandlerTestSelectionSelector(SourceDurationsFile(), policy)
    record = HandlerSelectionRecord(selector, policy).handle(
        ModelSelectionRecordRequest(
            repo="OmniNode-ai/omnimarket",
            pr_number=7,
            head_sha="a" * 40,
            base_sha="b" * 40,
            checkout_sha="c" * 40,
            run_id=1,
            run_attempt=1,
            changed_files=tuple(changed),
            flag_enabled=True,
            stage_selections={0: decided, 1: decided},
            repo_root=str(REPO_ROOT),
            selector_version="1.0.0",
            policy_version="p" * 12,
        )
    )
    assert record.selection_stage == 1
    assert record.executed_selection == record.selection_by_stage[1]
    assert record.executed_selection.selected_paths == decided.selected_paths
    assert record.executed_selection.kind != "full_suite" or decided.is_full_suite


def test_selection_stage_zero_replay_equals_the_baseline_receipt_exactly() -> None:
    """Replaying the baseline receipt's pinned input at stage 0 gives its counterfactual, byte for byte."""
    receipt = json.loads(
        (REPO_ROOT / "config" / "test_selection_replay_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    request = ModelReplayRequest.model_validate(
        json.loads((REPO_ROOT / receipt["replay_input_path"]).read_text("utf-8"))
    )
    result = HandlerSelectionReplay().handle(request)
    assert isinstance(result, ModelReplayResult), result
    assert result.model_dump(mode="json") == receipt["counterfactual"]


# -- AC1: the figures are recomputed, a skipped stage is refused ------------------


def test_stage_receipt_recomputed_valid_receipt_passes() -> None:
    result = _validate(_baseline(), _receipt(), current=1, base=0, recorded=_recorded())
    assert result.valid, [v.model_dump() for v in result.violations]


def test_stage_receipt_recomputed_comparison_is_the_same_twice() -> None:
    """Same recorded set, same answer: two validations of the receipt agree on every violation and on none."""
    first = _validate(_baseline(), _receipt(), current=1, base=0, recorded=_recorded())
    second = _validate(_baseline(), _receipt(), current=1, base=0, recorded=_recorded())
    assert first == second


def test_stage_receipt_recomputed_wrong_figure_fails_although_well_formed() -> None:
    sha, held, _ = _decision_runs()
    run = ModelRecordedFullRun(
        sha=sha,
        readable=True,
        conclusion="failure",
        failing_tests=(f"{held}/test_x.py::test_y",),
    )
    receipt = _receipt()  # well formed, and it claims no failing test
    assert receipt.comparison is not None
    assert receipt.comparison.failing_tests == 0
    result = _validate(
        _baseline(), receipt, current=1, base=0, recorded=_with_runs(run)
    )
    assert not result.valid
    assert Code.FIGURE_MISMATCH in _codes(result)


def test_stage_receipt_recomputed_correcting_the_figure_makes_it_pass() -> None:
    sha, held, _ = _decision_runs()
    run = ModelRecordedFullRun(
        sha=sha,
        readable=True,
        conclusion="failure",
        failing_tests=(f"{held}/test_x.py::test_y",),
    )
    recorded = _with_runs(run)
    stale = _receipt()
    assert not _validate(_baseline(), stale, current=1, base=0, recorded=recorded).valid
    assert stale.comparison is not None
    corrected = stale.model_copy(
        update={
            "comparison": stale.comparison.model_copy(
                update={"failing_tests": 1, "failing_tests_selected": 1}
            )
        }
    )
    result = _validate(_baseline(), corrected, current=1, base=0, recorded=recorded)
    assert result.valid, [v.model_dump() for v in result.violations]


def test_stage_receipt_recomputed_share_or_total_that_differs_fails() -> None:
    receipt = _receipt()
    assert receipt.comparison is not None
    wrong = receipt.model_copy(
        update={
            "comparison": receipt.comparison.model_copy(
                update={"changes_narrowed": 12}
            ),
            "measured": receipt.measured.model_copy(  # type: ignore[union-attr]
                update={"merge_narrowed_share": 0.2}
            ),
        }
    )
    result = _validate(_baseline(), wrong, current=1, base=0, recorded=_recorded())
    assert _codes(result) == {Code.FIGURE_MISMATCH}
    detail = result.violations[0].detail
    assert "changes_narrowed 12 (recomputed 10)" in detail
    assert "merge_narrowed_share 0.2 (recomputed 0.166667)" in detail


def test_stage_receipt_recomputed_a_miss_fails_even_when_the_receipt_states_it() -> (
    None
):
    """A failing test the selection would not run is a miss; the receipt that admits it is still refused."""
    sha, _, outside = _decision_runs()
    run = ModelRecordedFullRun(
        sha=sha, readable=True, conclusion="failure", failing_tests=(outside,)
    )
    receipt = _receipt()
    assert receipt.comparison is not None
    admitted = receipt.model_copy(
        update={
            "comparison": receipt.comparison.model_copy(
                update={"failing_tests": 1, "failing_tests_selected": 0, "misses": 1}
            )
        }
    )
    result = _validate(
        _baseline(), admitted, current=1, base=0, recorded=_with_runs(run)
    )
    assert _codes(result) == {Code.COMPARISON_FAILED}
    assert "1 failing tests are not in their change's selection" in (
        result.violations[0].detail
    )


def test_stage_receipt_recomputed_inputs_must_equal_the_recorded_set_by_digest() -> (
    None
):
    receipt = _receipt()
    assert receipt.comparison is not None
    swapped = receipt.model_copy(
        update={
            "comparison": receipt.comparison.model_copy(
                update={"observed_input_sha256": "0" * 64}
            )
        }
    )
    result = _validate(_baseline(), swapped, current=1, base=0, recorded=_recorded())
    assert _codes(result) == {Code.INPUT_MISMATCH}


def test_stage_receipt_recomputed_selector_version_must_equal_this_trees() -> None:
    result = _validate(
        _baseline(),
        _receipt(selector_version="9.9.9"),
        current=1,
        base=0,
        recorded=_recorded(),
    )
    assert _codes(result) == {Code.VERSION_MISMATCH}


def test_stage_receipt_recomputed_unreadable_set_or_no_readable_run_is_never_a_pass() -> (
    None
):
    unreadable = _validate(_baseline(), _receipt(), current=1, base=0, recorded=None)
    assert Code.RECORDED_SET_UNAVAILABLE in _codes(unreadable)
    recorded = _recorded()
    none_readable = recorded.model_copy(
        update={
            "runs": tuple(
                ModelRecordedFullRun(sha=run.sha, readable=False)
                for run in recorded.runs
            )
        }
    )
    result = _validate(
        _baseline(), _receipt(), current=1, base=0, recorded=none_readable
    )
    assert Code.COMPARISON_FAILED in _codes(result)
    assert "no recorded full run is readable" in " ".join(
        v.detail for v in result.violations
    )


def test_stage_receipt_recomputed_a_failing_run_that_names_no_test_is_refused() -> None:
    sha, _, _ = _decision_runs()
    run = ModelRecordedFullRun(sha=sha, readable=True, conclusion="failure")
    result = _validate(
        _baseline(), _receipt(), current=1, base=0, recorded=_with_runs(run)
    )
    assert Code.COMPARISON_FAILED in _codes(result)
    assert "failing runs name no failing test" in " ".join(
        v.detail for v in result.violations
    )


def test_stage_receipt_recomputed_a_receipt_with_no_comparison_is_refused() -> None:
    result = _validate(
        _baseline(),
        _receipt(comparison=None),
        current=1,
        base=0,
        recorded=_recorded(),
    )
    assert _codes(result) == {Code.COMPARISON_ABSENT}


def test_selection_stage_two_stage_jump_fails() -> None:
    jump = _receipt(stage=2, entered_from_stage=1)
    # the base held stage 0 and the change names stage 2 with the evidence for stage 2 only
    result = _validate(_baseline(), jump, current=2, base=0, recorded=_recorded())
    assert Code.STAGE_JUMP in _codes(result)
    assert Code.RECEIPT_MISSING in _codes(result)  # no receipt names stage 1 either


def test_selection_stage_jump_is_refused_even_when_the_base_stage_is_unreadable() -> (
    None
):
    """With no base to compare against, the receipt of stage 2 must still enter from stage 1."""
    result = _validate(
        _baseline(),
        _receipt(stage=2, entered_from_stage=0),
        current=2,
        base=None,
        recorded=_recorded(),
    )
    assert Code.STAGE_JUMP in _codes(result)


def test_selection_stage_raise_without_a_receipt_for_the_new_stage_fails() -> None:
    result = _validate(_baseline(), current=1, base=0, recorded=_recorded())
    assert _codes(result) == {Code.RECEIPT_MISSING}
    assert result.violations[0].stage == 1


def test_selection_stage_lowering_the_stage_needs_no_new_evidence() -> None:
    """Rollback is the owner lowering the stage; the receipts above it need not be checked."""
    result = _validate(_baseline(), current=0, base=1, recorded=None)
    assert result.valid


# -- AC4: the measured targets ------------------------------------------------------


@pytest.mark.parametrize(
    "missing", ["merge_narrowed_share", "test_seconds_per_merge", "ci_minutes_per_pr"]
)
def test_selection_stage_receipt_lacking_a_measured_value_is_refused(
    missing: str,
) -> None:
    raw = _stage_one()
    raw["measured"].pop(missing)
    result = _validate(
        _baseline(),
        ModelStageReceipt.model_validate(raw),
        current=1,
        base=0,
        recorded=_recorded(),
    )
    assert _codes(result) == {Code.TARGETS_ABSENT}
    assert missing in result.violations[0].detail


def test_selection_stage_receipt_with_no_measured_section_is_refused() -> None:
    raw = _stage_one()
    raw.pop("measured")
    result = _validate(
        _baseline(),
        ModelStageReceipt.model_validate(raw),
        current=1,
        base=0,
        recorded=_recorded(),
    )
    assert _codes(result) == {Code.TARGETS_ABSENT}


# -- AC3: CODEOWNERS ----------------------------------------------------------------


def _owners_of(text: str, path: str) -> list[str] | None:
    """The owners of the last entry that covers ``path`` (an exact file, or a directory ending in a slash)."""
    owners: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pattern, *handles = stripped.split()
        anchored = pattern.lstrip("/")
        covers = path == anchored or (
            anchored.endswith("/") and path.startswith(anchored)
        )
        if covers and handles:
            owners = handles
    return owners


def test_selection_stage_codeowners_names_the_owner_on_the_three_gate_paths() -> None:
    text = CODEOWNERS.read_text(encoding="utf-8")
    for path in GATE_PATHS:
        assert _owners_of(text, path) == [OWNER_TEAM], path


@pytest.mark.parametrize("removed", GATE_PATHS)
def test_selection_stage_codeowners_without_one_entry_fails(removed: str) -> None:
    text = CODEOWNERS.read_text(encoding="utf-8")
    kept = "\n".join(
        line
        for line in text.splitlines()
        if line.strip().lstrip("/").split(" ")[0] != removed
    )
    assert kept != text
    assert _owners_of(kept, removed) is None


# -- the gate reaches the tests that guard it -----------------------------------------


def _select(changed: str) -> ModelTestSelection:
    return compute_selection(
        changed_files=[changed],
        adjacency_path=ADJACENCY,
        ref_name="feature/branch",
        event_name="pull_request",
        feature_flag_enabled=True,
    )


def test_selection_stage_change_to_the_policy_selects_this_test() -> None:
    assert (
        "tests/ci/test_selection_stage_evidence.py"
        in _select(POLICY_REL).selected_paths
    )


def test_selection_stage_change_to_a_receipt_runs_the_whole_tree_with_this_test() -> (
    None
):
    selection = _select("config/test_selection_stage_receipts/stage_1.yaml")
    assert selection.is_full_suite
    assert selection.selected_paths == ["tests/"]


# -- AC5: the old comments ----------------------------------------------------------


def _adjacency_comment_text() -> str:
    lines = ADJACENCY.read_text(encoding="utf-8").splitlines()
    comments = [
        line.lstrip("# ").strip() for line in lines if line.lstrip().startswith("#")
    ]
    return " ".join(comments)


@pytest.mark.parametrize(
    "stale",
    [
        "entry point change edits pyproject.toml, which is test infrastructure below and takes the full suite",
        "reaches no test directory and used to take the full suite",
    ],
)
def test_selection_stage_old_comment_no_longer_says_data_files_or_entry_points_take_the_full_suite(
    stale: str,
) -> None:
    assert stale not in " ".join(_adjacency_comment_text().split())


def test_selection_stage_comments_say_the_stage_sets_what_a_data_change_runs() -> None:
    text = " ".join(_adjacency_comment_text().split())
    assert (
        "selection_stage in the selector's policy.yaml rises one stage at a time"
        in text
    )


def test_selection_stage_base_without_the_key_counts_as_stage_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clone whose base branch predates the stage gate holds a policy.yaml with no selection_stage."""
    shown = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="target_shard_seconds: 360\n"
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return shown

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _base_stage() == 0
