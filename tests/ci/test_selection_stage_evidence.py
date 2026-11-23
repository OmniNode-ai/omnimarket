# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stage gate: a stage of test selection is entered one at a time, on evidence recomputed from published records.

Reads the real ``policy.yaml``, the receipts under ``config/test_selection_stage_receipts/`` and the
repository's CODEOWNERS file, and runs the selector node's ``validate_stage_receipt`` operation over them.
Fixture shapes (valid, short, wrong figure, wrong version, two-stage jump) live in
``tests/ci/fixtures/selection_stage/``. The records a receipt is checked against come from a record source,
never from a file the same change supplies: the tree's own stage is checked through the gateway adapter, which
reads each night's published record as a check run through the repository gateway node, so a tree whose stage
is above 0 passes only on records the nightly published and fails closed with ``records_unavailable`` when the
gateway cannot be read.
"""

from __future__ import annotations

import functools
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
    selector_version,
)
from omnimarket.models.model_github_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.gateway_read_in_memory import (
    GatewayReadInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_attribution_records_gateway import (
    SourceAttributionRecordsGateway,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_attribution_records_in_memory import (
    SourceAttributionRecordsInMemory,
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
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_stage_comparison import (
    HandlerStageComparison,
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
from omnimarket.nodes.node_test_selection_selector_compute.models.model_recorded_set import (
    ModelRecordedSet,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_selection_replay import (
    ModelReplayRequest,
    ModelReplayResult,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_comparison import (
    ModelRecordedFullRun,
    ModelStageComparisonRequest,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    EnumStageReceiptViolation as Code,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_stage_receipt import (
    ModelMeasuredTargets,
    ModelReceiptComparison,
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
REPLAY_INPUT = "config/test_selection_replay_input.json"
OBSERVED_INPUT = "config/test_selection_observed_input.json"


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


def _stage_one() -> ModelStageReceipt:
    """The stage 1 receipt this tree carries."""
    return ModelStageReceipt.model_validate(
        yaml.safe_load((RECEIPTS_DIR / "stage_1.yaml").read_text(encoding="utf-8"))
    )


@functools.cache
def _recorded_set() -> ModelRecordedSet:
    recorded = SourceRecordedSetFile(REPO_ROOT).read_recorded_set(
        REPLAY_INPUT, OBSERVED_INPUT
    )
    assert recorded is not None
    return recorded


@functools.cache
def _replayed() -> ModelReplayResult:
    """The replay of the recorded merges, run once: it takes seconds and every set here replays the same merges."""
    replayed = HandlerSelectionReplay().handle(_recorded_set().replay_input)
    assert isinstance(replayed, ModelReplayResult), replayed
    return replayed


class _ReplayOnce(HandlerSelectionReplay):
    """The replay handler answering from the one replay above, for validations over the same recorded merges."""

    def handle(self, request: ModelReplayRequest) -> ModelReplayResult:
        assert request == _recorded_set().replay_input
        return _replayed()


def _with_failing_run(recorded: ModelRecordedSet, test: str) -> ModelRecordedSet:
    """The recorded set with one narrowed change's full run failing the given test."""
    narrowed = next(d for d in _replayed().decisions if d.kind == "narrowed")
    runs = (
        *(r for r in recorded.runs if r.sha != narrowed.sha),
        ModelRecordedFullRun(
            sha=narrowed.sha,
            readable=True,
            conclusion="failure",
            failing_tests=(test,),
        ),
    )
    return recorded.model_copy(update={"runs": runs})


def _receipt_matching(recorded: ModelRecordedSet) -> ModelStageReceipt:
    """The stage 1 receipt whose figures are what the comparison computes over the set."""
    result = HandlerStageComparison().handle(
        ModelStageComparisonRequest(
            replay=_replayed(), runs=recorded.runs, min_changes_compared=1
        )
    )
    return ModelStageReceipt(
        stage=1,
        entered_from_stage=0,
        repository="OmniNode-ai/omnimarket",
        selector_version=selector_version(),
        comparison=ModelReceiptComparison(
            replay_input_path=REPLAY_INPUT,
            replay_input_sha256=recorded.replay_input_sha256,
            observed_input_path=OBSERVED_INPUT,
            observed_input_sha256=recorded.observed_input_sha256,
            changes_compared=result.changes_compared,
            changes_narrowed=result.changes_narrowed,
            runs_with_result=result.runs_with_result,
            runs_unavailable=result.runs_unavailable,
            failing_tests=result.failing_tests,
            failing_tests_selected=result.failing_tests_selected,
            misses=len(result.misses),
        ),
        measured=ModelMeasuredTargets(
            merge_narrowed_share=result.merge_narrowed_share,
            test_seconds_per_merge=result.test_seconds_per_merge,
            ci_minutes_per_pr=result.ci_minutes_per_pr,
        ),
    )


class _AttributionSourceSpy:
    """An attribution record source that records every read; stage 1 must make none."""

    def __init__(self) -> None:
        self.reads = 0

    def read_records(self, repository: str, nights: Any) -> None:
        self.reads += 1


def _validate_stage_one(
    receipt: ModelStageReceipt,
    recorded: ModelRecordedSet | None,
    spy: _AttributionSourceSpy | None = None,
    policy: Any = None,
) -> ModelStageReceiptValidationResult:
    handler = HandlerStageReceiptValidation(
        spy or SourceAttributionRecordsInMemory(None),
        policy or load_policy(),
        recorded_source=SourceRecordedSetInMemory(recorded),
        replay=_ReplayOnce(),
    )
    return handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=1, base_stage=0, receipts=(_baseline(), receipt)
        )
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
    """The real policy, the real receipts, and the gateway adapter as the record source."""
    policy = load_policy()
    stage = policy.selection_stage
    # The gateway client is built here and reads nothing at stage 1, which is entered on one comparison
    # recomputed over the recorded set; only a stage above 1 reads the published nightly records.
    handler = HandlerStageReceiptValidation(
        SourceAttributionRecordsGateway(policy=policy),
        policy,
        recorded_source=SourceRecordedSetFile(REPO_ROOT),
    )
    result = handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=stage, base_stage=_base_stage(), receipts=_tree_receipts()
        )
    )
    assert stage == 1
    assert result.valid, [v.model_dump() for v in result.violations]


# -- the gateway adapter as the record source (AC1, AC4) ----------------------------------


def _gateway_with(
    records: list[ModelPublishedAttributionRecord],
) -> GatewayReadInMemory:
    gateway = GatewayReadInMemory()
    for r in records:
        gateway.publish(
            r.tested_head_sha,
            f"{load_policy().record_check_name_prefix}:{r.nightly_slot_date.isoformat()}",
            r.model_dump_json(),
        )
    return gateway


def _validate_through_gateway(
    gateway: GatewayReadInMemory,
) -> ModelStageReceiptValidationResult:
    handler = HandlerStageReceiptValidation(
        SourceAttributionRecordsGateway(gateway, load_policy()), load_policy()
    )
    return handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=2,
            base_stage=1,
            receipts=(_baseline(), _stage_one(), _receipt("receipt_valid.json")),
        )
    )


def test_selection_stage_attribution_source_seven_clean_published_records_pass() -> (
    None
):
    """Stage 2, a valid receipt and seven complete clean records published as check runs: the gate passes."""
    result = _validate_through_gateway(_gateway_with(_records("records_valid.json")))
    assert result.valid, [v.model_dump() for v in result.violations]


def test_selection_stage_attribution_source_one_night_check_run_absent_fails() -> None:
    result = _validate_through_gateway(
        _gateway_with(_records("records_valid.json")[:-1])
    )
    assert not result.valid
    assert Code.RECORD_MISSING in _codes(result)


def test_selection_stage_attribution_source_unreachable_gateway_fails_closed() -> None:
    gateway = _gateway_with(_records("records_valid.json"))
    gateway.unreachable = True
    result = _validate_through_gateway(gateway)
    assert _codes(result) == {Code.RECORDS_UNAVAILABLE}


def test_selection_stage_attribution_source_reads_nothing_at_stage_zero() -> None:
    gateway = GatewayReadInMemory()
    handler = HandlerStageReceiptValidation(
        SourceAttributionRecordsGateway(gateway, load_policy()), load_policy()
    )
    result = handler.handle(
        ModelStageReceiptValidationRequest(
            current_stage=0, base_stage=0, receipts=(_baseline(),)
        )
    )
    assert result.valid
    assert gateway.requests == []


# -- AC4: the adapter reads only through the gateway node operation, named in the contract ---


SELECTOR_NODE = (
    REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_test_selection_selector_compute"
)
_FORBIDDEN_IN_THE_READ_PATH = (
    "subprocess",
    "os.system",
    "requests",
    "httpx",
    "urllib",
    "aiohttp",
    "http.client",
    "socket",
    "gh api",
    '"gh"',
)


def test_selection_stage_attribution_source_contract_binding_names_the_handler_route() -> (
    None
):
    contract = yaml.safe_load((SELECTOR_NODE / "contract.yaml").read_text("utf-8"))
    routes = {
        entry["operation"]: entry for entry in contract["handler_routing"]["handlers"]
    }
    entry = routes["read_attribution_records"]
    assert entry["handler"] == {
        "name": "HandlerAttributionRecordRead",
        "module": "omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_attribution_record_read",
    }
    assert entry["input_model"].endswith(".ModelAttributionRecordReadRequest")
    assert entry["output_model"].endswith(".ModelAttributionRecordReading")
    description = " ".join(contract["description"].split())
    assert "commit_check_runs" in description
    assert "node_github_repo_gateway_effect" in description


def test_selection_stage_attribution_source_contract_binding_reads_only_the_gateway_operation() -> (
    None
):
    """No other gateway operation, no process, no network client and no ``gh`` in the files that read the records."""
    paths = [
        SELECTOR_NODE / "handlers" / "handler_attribution_record_read.py",
        SELECTOR_NODE / "adapters" / "source_attribution_records_gateway.py",
    ]
    operations = set()
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for needle in _FORBIDDEN_IN_THE_READ_PATH:
            assert needle not in text, (path.name, needle)
        operations |= {
            name for name in dir(EnumGithubGatewayOperation) if f".{name}" in text
        }
    assert operations == {"COMMIT_CHECK_RUNS"}


def test_selection_stage_attribution_source_contract_binding_adds_no_script() -> None:
    for script in (REPO_ROOT / "scripts").rglob("*"):
        if script.is_file() and script.suffix in {".py", ".sh"}:
            assert "SourceAttributionRecordsGateway" not in script.read_text(
                encoding="utf-8", errors="ignore"
            ), script.name


def test_selection_stage_attribution_source_request_asks_for_the_summary() -> None:
    gateway = _gateway_with(_records("records_valid.json"))
    _validate_through_gateway(gateway)
    assert all(isinstance(r, ModelGithubGatewayRequest) for r in gateway.requests)
    assert {r.include_output for r in gateway.requests} == {True}


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
    """The stage 0 baseline stays as history beside the receipts of the stages entered after it."""
    receipts = _tree_receipts()
    assert [r.stage for r in receipts if r.stage == 0] == [0]
    assert receipts[0] == _baseline()
    assert [r.stage for r in receipts] == list(range(load_policy().selection_stage + 1))


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
    """The record built at stage 0 runs exactly what today's selector decides, from the checked-in durations.

    A replay of a fixture at stage 0, whatever stage the tree holds now: the stage 0 baseline is history.
    """
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
    result = _validate(
        _baseline(),
        _stage_one(),
        _receipt("receipt_valid.json"),
        current=2,
        base=1,
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
        _stage_one(),
        receipt,
        current=2,
        base=1,
        records=_records("records_one_night_dirty.json"),
    )
    assert not result.valid
    assert {Code.FIGURE_MISMATCH, Code.NIGHT_NOT_CLEAN} <= _codes(result)


def test_stage_receipt_recomputed_correcting_the_figure_makes_it_pass() -> None:
    wrong = _receipt("receipt_wrong_figure.json")
    records = _records("records_valid.json")
    assert not _validate(
        _baseline(), _stage_one(), wrong, current=2, base=1, records=records
    ).valid
    corrected = wrong.model_copy(
        update={"pr_record_count": 14, "clean_night_share": 1.0}
    )
    result = _validate(
        _baseline(), _stage_one(), corrected, current=2, base=1, records=records
    )
    assert result.valid, [v.model_dump() for v in result.violations]


def test_stage_receipt_recomputed_a_receipt_naming_fewer_nights_is_short() -> None:
    result = _validate(
        _baseline(),
        _stage_one(),
        _receipt("receipt_short.json"),
        current=2,
        base=1,
        records=_records("records_valid.json"),
    )
    assert Code.NIGHTS_SHORT in _codes(result)


def test_stage_receipt_recomputed_share_or_total_that_differs_from_the_records_fails() -> (
    None
):
    receipt = _receipt("receipt_wrong_figure.json")
    result = _validate(
        _baseline(),
        _stage_one(),
        receipt,
        current=2,
        base=1,
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
        r.model_copy(update={"pull_request_record_count": 1}) for r in supplied
    ]  # the source says 7 records in all
    result = _validate(
        _baseline(),
        _stage_one(),
        _receipt("receipt_valid.json"),
        current=2,
        base=1,
        records=published,
    )
    assert Code.FIGURE_MISMATCH in _codes(result)
    assert Code.TOO_FEW_PR_RECORDS in _codes(result)


def test_stage_receipt_recomputed_versions_must_equal_those_in_the_records() -> None:
    result = _validate(
        _baseline(),
        _stage_one(),
        _receipt("receipt_wrong_version.json"),
        current=2,
        base=1,
        records=_records("records_valid.json"),
    )
    assert _codes(result) == {Code.VERSION_MISMATCH}
    selector_wrong = _receipt("receipt_valid.json", selector_version="9.9.9")
    result = _validate(
        _baseline(),
        _stage_one(),
        selector_wrong,
        current=2,
        base=1,
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
        _stage_one(),
        _receipt("receipt_valid.json"),
        current=2,
        base=1,
        records=wrong_entry,
    )
    assert _codes(result) == {Code.RECORD_WRONG_STAGE_ENTRY}


def test_stage_receipt_recomputed_unreadable_or_missing_records_are_never_a_pass() -> (
    None
):
    unreadable = _validate(
        _baseline(),
        _stage_one(),
        _receipt("receipt_valid.json"),
        current=2,
        base=1,
        records=None,
    )
    assert Code.RECORDS_UNAVAILABLE in _codes(unreadable)
    missing = _validate(
        _baseline(),
        _stage_one(),
        _receipt("receipt_valid.json"),
        current=2,
        base=1,
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
        _stage_one(),
        ModelStageReceipt.model_validate(raw),
        current=2,
        base=1,
        records=_records("records_valid.json"),
    )
    assert _codes(result) == {Code.TARGETS_ABSENT}
    assert missing in result.violations[0].detail


def test_selection_stage_receipt_with_no_measured_section_is_refused() -> None:
    raw = _load("receipt_valid.json")
    raw.pop("measured")
    result = _validate(
        _baseline(),
        _stage_one(),
        ModelStageReceipt.model_validate(raw),
        current=2,
        base=1,
        records=_records("records_valid.json"),
    )
    assert _codes(result) == {Code.TARGETS_ABSENT}


# -- stage 1: entered on one comparison recomputed over the recorded set (AC1, AC4) ------------


def test_stage_receipt_recomputed_stage_one_receipt_of_this_tree_passes_on_the_recorded_set() -> (
    None
):
    receipt = _stage_one()
    assert receipt.comparison is not None
    assert receipt.comparison.changes_compared == 60
    result = _validate_stage_one(receipt, _recorded_set())
    assert result.valid, [v.model_dump() for v in result.violations]


def test_stage_receipt_recomputed_stage_one_figures_equal_what_the_comparison_computes() -> (
    None
):
    assert _stage_one() == _receipt_matching(_recorded_set())


def test_stage_receipt_recomputed_stage_one_never_reads_the_nightly_records() -> None:
    spy = _AttributionSourceSpy()
    result = _validate_stage_one(_stage_one(), _recorded_set(), spy)
    assert result.valid
    assert spy.reads == 0


@pytest.mark.parametrize(
    "figure", ["merge_narrowed_share", "test_seconds_per_merge", "ci_minutes_per_pr"]
)
def test_stage_receipt_recomputed_stage_one_wrong_measured_figure_fails_although_well_formed(
    figure: str,
) -> None:
    receipt = _stage_one()
    assert receipt.measured is not None
    wrong = receipt.model_copy(
        update={
            "measured": receipt.measured.model_copy(
                update={figure: getattr(receipt.measured, figure) + 1.0}
                if figure != "merge_narrowed_share"
                else {figure: 0.5}
            )
        }
    )
    result = _validate_stage_one(wrong, _recorded_set())
    assert _codes(result) == {Code.FIGURE_MISMATCH}
    assert figure in result.violations[0].detail
    # correct the figure and it passes
    assert _validate_stage_one(receipt, _recorded_set()).valid


def test_stage_receipt_recomputed_stage_one_wrong_comparison_count_fails() -> None:
    receipt = _stage_one()
    assert receipt.comparison is not None
    wrong = receipt.model_copy(
        update={
            "comparison": receipt.comparison.model_copy(
                update={"changes_narrowed": receipt.comparison.changes_narrowed + 1}
            )
        }
    )
    result = _validate_stage_one(wrong, _recorded_set())
    assert _codes(result) == {Code.FIGURE_MISMATCH}
    assert "changes_narrowed" in result.violations[0].detail


_MISSED = "tests/test_a_file_no_narrowed_selection_holds.py::test_it"


def test_stage_receipt_recomputed_stage_one_a_failing_test_missing_from_the_selection_is_refused() -> (
    None
):
    """The positive control: a recorded full run that failed a test the selection leaves out refuses the stage."""
    failing = _with_failing_run(_recorded_set(), _MISSED)
    receipt = _receipt_matching(failing)
    assert receipt.comparison is not None
    assert receipt.comparison.misses == 1  # the receipt is honest about the miss
    result = _validate_stage_one(receipt, failing)
    assert _codes(result) == {Code.COMPARISON_MISS}
    assert _MISSED in result.violations[0].detail


def test_stage_receipt_recomputed_stage_one_a_receipt_claiming_no_miss_is_still_refused() -> (
    None
):
    failing = _with_failing_run(_recorded_set(), _MISSED)
    result = _validate_stage_one(_stage_one(), failing)
    assert {Code.COMPARISON_MISS, Code.FIGURE_MISMATCH} <= _codes(result)


def test_stage_receipt_recomputed_stage_one_a_failing_test_the_selection_runs_is_not_a_miss() -> (
    None
):
    recorded = _recorded_set()
    narrowed = next(d for d in _replayed().decisions if d.kind == "narrowed")
    held = narrowed.selected_paths[0].rstrip("/") + "/test_x.py::test_it"
    failing = _with_failing_run(recorded, held)
    result = _validate_stage_one(_receipt_matching(failing), failing)
    assert result.valid, [v.model_dump() for v in result.violations]


def test_stage_receipt_recomputed_stage_one_without_a_comparison_or_set_is_never_a_pass() -> (
    None
):
    bare = _stage_one().model_copy(update={"comparison": None})
    assert _codes(_validate_stage_one(bare, _recorded_set())) == {
        Code.COMPARISON_ABSENT
    }
    unreadable = _validate_stage_one(_stage_one(), None)
    assert _codes(unreadable) == {Code.RECORDED_SET_UNAVAILABLE}


def test_stage_receipt_recomputed_stage_one_digest_of_the_inputs_must_match() -> None:
    changed = _recorded_set().model_copy(update={"observed_input_sha256": "0" * 64})
    result = _validate_stage_one(_stage_one(), changed)
    assert _codes(result) == {Code.INPUT_MISMATCH}


def test_stage_receipt_recomputed_stage_one_selector_version_must_be_this_trees() -> (
    None
):
    wrong = _stage_one().model_copy(update={"selector_version": "9.9.9"})
    result = _validate_stage_one(wrong, _recorded_set())
    assert _codes(result) == {Code.VERSION_MISMATCH}


def test_stage_receipt_recomputed_stage_one_comparison_over_too_few_changes_fails() -> (
    None
):
    policy = load_policy().model_copy(update={"stage_min_compared_changes": 61})
    result = _validate_stage_one(_stage_one(), _recorded_set(), policy=policy)
    assert _codes(result) == {Code.COMPARISON_FAILED}


def test_stage_receipt_recomputed_stage_one_jump_from_zero_to_two_still_fails() -> None:
    result = _validate(
        _baseline(),
        _stage_one(),
        _receipt("receipt_valid.json", entered_from_stage=0),
        current=2,
        base=0,
        records=_records("records_valid.json"),
    )
    assert Code.STAGE_JUMP in _codes(result)


@pytest.mark.parametrize(
    "missing", ["merge_narrowed_share", "test_seconds_per_merge", "ci_minutes_per_pr"]
)
def test_selection_stage_stage_one_receipt_lacking_a_measured_value_is_refused(
    missing: str,
) -> None:
    receipt = _stage_one()
    assert receipt.measured is not None
    lacking = receipt.model_copy(
        update={"measured": receipt.measured.model_copy(update={missing: None})}
    )
    result = _validate_stage_one(lacking, _recorded_set())
    assert Code.TARGETS_ABSENT in _codes(result)
    assert missing in next(
        v.detail for v in result.violations if v.code is Code.TARGETS_ABSENT
    )


def test_selection_stage_stage_one_receipt_carries_no_nights() -> None:
    with pytest.raises(ValueError, match="nights"):
        ModelStageReceipt.model_validate(
            {**_load("receipt_valid.json"), "stage": 1, "entered_from_stage": 0}
        )


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
