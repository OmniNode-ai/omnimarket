# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stage gate: a stage of test selection is entered one at a time, on evidence recomputed from published records.

Reads the real ``policy.yaml``, the receipts under ``config/test_selection_stage_receipts/`` and the
repository's CODEOWNERS file, and runs the selector node's ``validate_stage_receipt`` operation over them.
Fixture shapes (valid, short, wrong figure, wrong version, two-stage jump) live in
``tests/ci/fixtures/selection_stage/``. The records a receipt is checked against come from a record source,
never from a file the same change supplies: the source adapter for the published records is not wired yet, so
a tree whose stage is above 0 fails closed with ``records_unavailable`` until it is.
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
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_attribution_records_in_memory import (
    SourceAttributionRecordsInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_durations_file import (
    SourceDurationsFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_record import (
    HandlerSelectionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_receipt_validation import (
    HandlerStageReceiptValidation,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_published_attribution_record import (
    ModelPublishedAttributionRecord,
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
FIXTURES = Path(__file__).parent / "fixtures" / "selection_stage"
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"
ADJACENCY = REPO_ROOT / "scripts" / "ci" / "test_selection_adjacency.yaml"
POLICY_REL = POLICY_PATH.relative_to(REPO_ROOT).as_posix()
GATE_PATHS = (
    POLICY_REL,
    "config/test_selection_stage_receipts/",
    "tests/ci/test_selection_stage_evidence.py",
)
OWNER_TEAM = "@OmniNode-ai/platform-leads"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _records(name: str) -> list[ModelPublishedAttributionRecord]:
    return [ModelPublishedAttributionRecord.model_validate(r) for r in _load(name)]


def _receipt(name: str, **changes: Any) -> ModelStageReceipt:
    return ModelStageReceipt.model_validate({**_load(name), **changes})


def _baseline() -> ModelStageReceipt:
    return ModelStageReceipt(stage=0)


def _validate(
    *receipts: ModelStageReceipt,
    current: int,
    base: int | None,
    records: list[ModelPublishedAttributionRecord] | None,
) -> ModelStageReceiptValidationResult:
    handler = HandlerStageReceiptValidation(
        SourceAttributionRecordsInMemory(records), load_policy()
    )
    return handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=current, base_stage=base, receipts=tuple(receipts)
        )
    )


def _codes(result: ModelStageReceiptValidationResult) -> set[Code]:
    return {violation.code for violation in result.violations}


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
    """The real policy, the real receipts, and the record source as wired today."""
    stage = load_policy().selection_stage
    result = _validate(
        *_tree_receipts(), current=stage, base=_base_stage(), records=None
    )
    assert result.valid, [v.model_dump() for v in result.violations]


def test_selection_stage_policy_holds_the_gate_settings() -> None:
    policy = load_policy()
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert raw["stage_clean_nights"] == policy.stage_clean_nights == 7
    assert policy.stage_min_pr_records >= 1
    comments = POLICY_PATH.read_text(encoding="utf-8")
    assert "#   stage_clean_nights " in comments
    assert "#   stage_min_pr_records " in comments


def test_selection_stage_zero_baseline_receipt_is_the_only_receipt_at_stage_zero() -> (
    None
):
    receipts = _tree_receipts()
    assert load_policy().selection_stage == 0
    assert [r.stage for r in receipts] == [0]
    assert receipts[0] == _baseline()


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
    policy = load_policy()
    assert policy.selection_stage == 0
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


# -- AC1: the figures are recomputed, a skipped stage is refused ------------------


def test_stage_receipt_recomputed_valid_receipt_passes() -> None:
    result = _validate(
        _baseline(),
        _receipt("receipt_valid.json"),
        current=1,
        base=0,
        records=_records("records_valid.json"),
    )
    assert result.valid, [v.model_dump() for v in result.violations]


def test_stage_receipt_recomputed_one_clean_night_short_fails_although_well_formed() -> (
    None
):
    receipt = _receipt("receipt_claims_seven_clean.json")
    assert receipt.clean_nights == 7  # well formed, and it claims seven clean nights
    result = _validate(
        _baseline(),
        receipt,
        current=1,
        base=0,
        records=_records("records_one_night_dirty.json"),
    )
    assert not result.valid
    assert {Code.FIGURE_MISMATCH, Code.NIGHT_NOT_CLEAN} <= _codes(result)


def test_stage_receipt_recomputed_correcting_the_figure_makes_it_pass() -> None:
    wrong = _receipt("receipt_wrong_figure.json")
    records = _records("records_valid.json")
    assert not _validate(_baseline(), wrong, current=1, base=0, records=records).valid
    corrected = wrong.model_copy(
        update={"pr_record_count": 14, "clean_night_share": 1.0}
    )
    result = _validate(_baseline(), corrected, current=1, base=0, records=records)
    assert result.valid, [v.model_dump() for v in result.violations]


def test_stage_receipt_recomputed_a_receipt_naming_fewer_nights_is_short() -> None:
    result = _validate(
        _baseline(),
        _receipt("receipt_short.json"),
        current=1,
        base=0,
        records=_records("records_valid.json"),
    )
    assert Code.NIGHTS_SHORT in _codes(result)


def test_stage_receipt_recomputed_share_or_total_that_differs_from_the_records_fails() -> (
    None
):
    receipt = _receipt("receipt_wrong_figure.json")
    result = _validate(
        _baseline(),
        receipt,
        current=1,
        base=0,
        records=_records("records_valid.json"),
    )
    assert _codes(result) == {Code.FIGURE_MISMATCH}
    detail = result.violations[0].detail
    assert "pr_record_count 40 (recomputed 14)" in detail
    assert "clean_night_share 0.5 (recomputed 1.0)" in detail


def test_stage_receipt_recomputed_totals_come_from_the_source_not_the_supplied_file() -> (
    None
):
    """A receipt that agrees with a records file the change supplies still fails against the source's records."""
    supplied = _records("records_valid.json")
    published = [
        r.model_copy(update={"pr_record_count": 1}) for r in supplied
    ]  # the source says 7 records in all
    result = _validate(
        _baseline(),
        _receipt("receipt_valid.json"),
        current=1,
        base=0,
        records=published,
    )
    assert Code.FIGURE_MISMATCH in _codes(result)
    assert Code.TOO_FEW_PR_RECORDS in _codes(result)


def test_stage_receipt_recomputed_versions_must_equal_those_in_the_records() -> None:
    result = _validate(
        _baseline(),
        _receipt("receipt_wrong_version.json"),
        current=1,
        base=0,
        records=_records("records_valid.json"),
    )
    assert _codes(result) == {Code.VERSION_MISMATCH}
    selector_wrong = _receipt("receipt_valid.json", selector_version="9.9.9")
    result = _validate(
        _baseline(),
        selector_wrong,
        current=1,
        base=0,
        records=_records("records_valid.json"),
    )
    assert _codes(result) == {Code.VERSION_MISMATCH}


def test_stage_receipt_recomputed_records_must_hold_the_entry_of_the_stage_entered() -> (
    None
):
    wrong_entry = [
        r.model_copy(update={"stage_entry_counted": 0})
        for r in _records("records_valid.json")
    ]
    result = _validate(
        _baseline(),
        _receipt("receipt_valid.json"),
        current=1,
        base=0,
        records=wrong_entry,
    )
    assert _codes(result) == {Code.RECORD_WRONG_STAGE_ENTRY}


def test_stage_receipt_recomputed_unreadable_or_missing_records_are_never_a_pass() -> (
    None
):
    unreadable = _validate(
        _baseline(),
        _receipt("receipt_valid.json"),
        current=1,
        base=0,
        records=None,
    )
    assert Code.RECORDS_UNAVAILABLE in _codes(unreadable)
    missing = _validate(
        _baseline(),
        _receipt("receipt_valid.json"),
        current=1,
        base=0,
        records=_records("records_valid.json")[:-1],
    )
    assert Code.RECORD_MISSING in _codes(missing)


def test_selection_stage_two_stage_jump_fails() -> None:
    jump = _receipt("receipt_two_stage_jump.json")
    # the base held stage 0 and the change names stage 2 with the evidence for stage 2 only
    result = _validate(
        _baseline(),
        jump,
        current=2,
        base=0,
        records=_records("records_two_stage_jump.json"),
    )
    assert Code.STAGE_JUMP in _codes(result)
    assert Code.RECEIPT_MISSING in _codes(result)  # no receipt names stage 1 either


def test_selection_stage_jump_is_refused_even_when_the_base_stage_is_unreadable() -> (
    None
):
    """With no base to compare against, the receipt of stage 2 must still enter from stage 1."""
    result = _validate(
        _baseline(),
        _receipt("receipt_two_stage_jump.json"),
        current=2,
        base=None,
        records=_records("records_two_stage_jump.json"),
    )
    assert Code.STAGE_JUMP in _codes(result)


def test_selection_stage_raise_without_a_receipt_for_the_new_stage_fails() -> None:
    result = _validate(
        _baseline(), current=1, base=0, records=_records("records_valid.json")
    )
    assert _codes(result) == {Code.RECEIPT_MISSING}
    assert result.violations[0].stage == 1


def test_selection_stage_lowering_the_stage_needs_no_new_evidence() -> None:
    """Rollback is the owner lowering the stage; the receipts above it need not be checked."""
    result = _validate(_baseline(), current=0, base=1, records=None)
    assert result.valid


# -- AC4: the measured targets ------------------------------------------------------


@pytest.mark.parametrize(
    "missing", ["merge_narrowed_share", "test_seconds_per_merge", "ci_minutes_per_pr"]
)
def test_selection_stage_receipt_lacking_a_measured_value_is_refused(
    missing: str,
) -> None:
    raw = _load("receipt_valid.json")
    raw["measured"].pop(missing)
    result = _validate(
        _baseline(),
        ModelStageReceipt.model_validate(raw),
        current=1,
        base=0,
        records=_records("records_valid.json"),
    )
    assert _codes(result) == {Code.TARGETS_ABSENT}
    assert missing in result.violations[0].detail


def test_selection_stage_receipt_with_no_measured_section_is_refused() -> None:
    raw = _load("receipt_valid.json")
    raw.pop("measured")
    result = _validate(
        _baseline(),
        ModelStageReceipt.model_validate(raw),
        current=1,
        base=0,
        records=_records("records_valid.json"),
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
