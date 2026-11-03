# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CM.10: identical inputs, isolated forks, explicit refusals and arm evidence."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

import omnimarket.nodes.node_compaction_arm_run_effect.handlers.handler_arm_run as arm_handler
from omnimarket.models.context_install_confirm import EnumBoundaryKind
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.handler_arm_run import (
    HandlerCompactionArmRun,
    LiveReadRefusedError,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    EnumArm,
    EnumArmStatus,
    EnumArmTrack,
    EnumPremise,
    EnumReplySource,
    EnumStockBranch,
    ModelAdapterOutput,
    ModelArmRecord,
    ModelArmRunRequest,
    ModelArmRunResult,
    ModelFork,
)

from .conftest import ScriptedAdapter, ScriptedPremises, digest, make_request

pytestmark = pytest.mark.unit


def _records(result: ModelArmRunResult, case_id: str) -> dict[EnumArm, ModelArmRecord]:
    records = [record for record in result.records if record.case_id == case_id]
    indexed = {record.arm: record for record in records}
    assert len(indexed) == len(records), "duplicate arm records for a case"
    return indexed


def _record_directory(request: ModelArmRunRequest, record: ModelArmRecord) -> Path:
    return (
        Path(request.run_root)
        / "records"
        / record.case_id
        / record.arm.value
        / f"r{record.repeat_index}"
    )


def _assert_refusal_branch(result: ModelArmRunResult) -> None:
    assert result.stock_rerun_refused is True
    assert result.stock_branch is EnumStockBranch.RECORDED_STOCK
    assert result.report_label == "RECORDED_STOCK"
    assert result.planted_stratum_safeguard == "NOT_ESTABLISHED"
    assert result.refusal_statement
    assert "stock_rerun" in result.refusal_statement
    assert "REFUSED" in result.refusal_statement


def test_arms_differ_only_at_compaction(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
    mod_adapter: ScriptedAdapter,
) -> None:
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod_adapter}
    ).handle(arm_request)
    assert result.removed_cases == ()
    assert len(result.records) == 6
    for case in arm_request.cases:
        records = _records(result, case.case_id)
        assert set(records) == set(arm_request.arms)
        assert len({record.fork_root for record in records.values()}) == len(records)
        assert all(
            record.status is EnumArmStatus.RECORDED for record in records.values()
        )
        for field in (
            "prefix_sha256",
            "ledger_sha256",
            "initial_state_hash",
            "compaction_input_hash",
        ):
            assert len({getattr(record, field) for record in records.values()}) == 1
        assert {record.prefix_sha256 for record in records.values()} == {
            sha256(Path(case.prefix_path).read_bytes()).hexdigest()
        }
        assert {record.ledger_sha256 for record in records.values()} == {
            sha256(Path(case.ledger_path).read_bytes()).hexdigest()
        }
    assert stock_adapter.call_count == mod_adapter.call_count == 2


def test_corrupt_mod_copy_removes_only_its_case_from_every_arm(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
    mod_adapter: ScriptedAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    affected, unaffected = arm_request.cases
    original = Path(affected.prefix_path).read_bytes()
    copy_file = arm_handler._copy_file
    corrupted: list[Path] = []

    def corrupt_mod_copy(src: Path, dst: Path) -> None:
        copy_file(src, dst)
        if src == Path(affected.prefix_path) and EnumArm.MOD.value in dst.parts:
            dst.write_bytes(original + b"corrupted copy\n")
            corrupted.append(dst)

    monkeypatch.setattr(arm_handler, "_copy_file", corrupt_mod_copy)
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod_adapter}
    ).handle(arm_request)
    assert len(corrupted) == 1, (
        "the negative control must actually corrupt the mod copy"
    )
    assert [(case.case_id, case.reason) for case in result.removed_cases] == [
        (affected.case_id, "FORK_HASH_MISMATCH")
    ]
    assert _records(result, affected.case_id) == {}
    remaining = _records(result, unaffected.case_id)
    assert set(remaining) == set(arm_request.arms)
    assert all(record.status is EnumArmStatus.RECORDED for record in remaining.values())
    assert {record.prefix_sha256 for record in remaining.values()} == {
        sha256(Path(unaffected.prefix_path).read_bytes()).hexdigest()
    }
    assert {record.ledger_sha256 for record in remaining.values()} == {
        sha256(Path(unaffected.ledger_path).read_bytes()).hexdigest()
    }
    assert len({record.initial_state_hash for record in remaining.values()}) == 1
    assert len({record.compaction_input_hash for record in remaining.values()}) == 1
    assert Path(affected.prefix_path).read_bytes() == original


def test_fork_isolation(tmp_path: Path, premises: ScriptedPremises) -> None:
    request = make_request(tmp_path, arms=(EnumArm.STOCK_RERUN, EnumArm.MOD))
    sources = {
        Path(path): Path(path).read_bytes()
        for case in request.cases
        for path in (case.prefix_path, case.ledger_path)
    }

    def stock_writes(fork: ModelFork) -> None:
        (Path(fork.root) / "stock-only.txt").write_text("stock private state")
        (Path(fork.config_dir) / "stock-config.txt").write_text("stock private config")

    def mod_checks(fork: ModelFork) -> None:
        assert not (Path(fork.root) / "stock-only.txt").exists()
        assert not (Path(fork.config_dir) / "stock-config.txt").exists()

    stock = ScriptedAdapter(
        ModelAdapterOutput(compacted_context="native"), effect=stock_writes
    )
    mod = ScriptedAdapter(
        ModelAdapterOutput(compacted_context="mod", reply_source=EnumReplySource.MOD),
        effect=mod_checks,
    )
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock, EnumArm.MOD: mod}
    ).handle(request)
    forks = stock.calls + mod.calls
    assert len(forks) == 4
    assert len({fork.root for fork in forks}) == 4
    assert len({fork.config_dir for fork in forks}) == 4
    assert all(Path(fork.root).is_dir() for fork in forks)
    assert all(Path(fork.config_dir).is_dir() for fork in forks)
    for fork in mod.calls:
        assert not (Path(fork.root) / "stock-only.txt").exists()
    assert {path: path.read_bytes() for path in sources} == sources
    for case in request.cases:
        records = _records(result, case.case_id)
        assert set(records) == set(request.arms)
        assert all(record.scorable for record in records.values())
        assert (
            records[EnumArm.STOCK_RERUN].initial_state_hash
            == records[EnumArm.MOD].initial_state_hash
        )


@pytest.mark.parametrize("mutating_arm", [EnumArm.STOCK_RERUN, EnumArm.MOD])
def test_prefix_mutated_removes_case_from_every_arm(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    mutating_arm: EnumArm,
) -> None:
    affected, unaffected = arm_request.cases

    def mutate_prefix(fork: ModelFork) -> None:
        if fork.case_id == affected.case_id:
            Path(fork.prefix_path).write_text("adapter overwrote the frozen prefix")

    stock = ScriptedAdapter(
        ModelAdapterOutput(compacted_context="native"),
        effect=mutate_prefix if mutating_arm is EnumArm.STOCK_RERUN else None,
    )
    mod = ScriptedAdapter(
        ModelAdapterOutput(compacted_context="mod", reply_source=EnumReplySource.MOD),
        effect=mutate_prefix if mutating_arm is EnumArm.MOD else None,
    )
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock, EnumArm.MOD: mod}
    ).handle(arm_request)
    assert [(case.case_id, case.reason) for case in result.removed_cases] == [
        (affected.case_id, "PREFIX_MUTATED")
    ]
    assert _records(result, affected.case_id) == {}
    remaining = _records(result, unaffected.case_id)
    assert set(remaining) == set(arm_request.arms)
    assert all(record.scorable for record in remaining.values())


@pytest.mark.parametrize("repeat_index", [0, 2])
def test_fork_rerun_restores_snapshot_and_appends_attempts(
    tmp_path: Path,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
    mod_adapter: ScriptedAdapter,
    repeat_index: int,
) -> None:
    request = make_request(tmp_path, repeat_index=repeat_index)
    handler = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod_adapter}
    )
    first = handler.handle(request)
    assert len(first.records) == 6
    original_records: dict[Path, bytes] = {}
    stray_files: list[Path] = []
    for record in first.records:
        assert record.attempt == 1
        assert record.repeat_index == repeat_index
        path = _record_directory(request, record) / "attempt-1.json"
        original_records[path] = path.read_bytes()
        assert ModelArmRecord.model_validate_json(original_records[path]) == record
        stray = Path(record.fork_root) / "stray-from-previous-attempt.txt"
        stray.write_text("must not enter the next snapshot")
        stray_files.append(stray)
    second = handler.handle(request)
    assert len(second.records) == 6
    assert second.removed_cases == ()
    assert all(not path.exists() for path in stray_files)
    assert {path: path.read_bytes() for path in original_records} == original_records
    for case in request.cases:
        before = _records(first, case.case_id)
        after = _records(second, case.case_id)
        assert set(before) == set(after) == set(request.arms)
        for arm, record in after.items():
            assert record.attempt == 2
            assert record.initial_state_hash == before[arm].initial_state_hash
            path = _record_directory(request, record) / "attempt-2.json"
            assert ModelArmRecord.model_validate_json(path.read_bytes()) == record


@pytest.mark.parametrize(
    ("boundary_kind", "evidence"),
    [
        (EnumBoundaryKind.CUSTOM, ()),
        (EnumBoundaryKind.NATIVE, ("custom replacement observed",)),
    ],
    ids=["custom-boundary", "native-with-custom-evidence"],
)
def test_custom_boundary_invalid(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    boundary_kind: EnumBoundaryKind,
    evidence: tuple[str, ...],
) -> None:
    stock = ScriptedAdapter(
        ModelAdapterOutput(
            compacted_context="must never be scored",
            boundary_kind=boundary_kind,
            custom_boundary_evidence=evidence,
        )
    )
    result = HandlerCompactionArmRun(premises, {EnumArm.STOCK_RERUN: stock}).handle(
        arm_request
    )
    for case in arm_request.cases:
        record = _records(result, case.case_id)[EnumArm.STOCK_RERUN]
        assert record.status is EnumArmStatus.INVALID
        assert record.reason == "CUSTOM_BOUNDARY_EVIDENCE"
        assert record.scorable is False
        assert record.compacted_context_sha256 is None
        assert not any(
            candidate.scorable and candidate.fork_root == record.fork_root
            for candidate in result.records
        )
    assert stock.call_count == 2


def test_native_stock_boundary_is_recorded_and_scorable(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
) -> None:
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter}
    ).handle(arm_request)
    for case in arm_request.cases:
        record = _records(result, case.case_id)[EnumArm.STOCK_RERUN]
        assert record.status is EnumArmStatus.RECORDED
        assert record.scorable is True
        assert record.reason is None
        assert record.boundary_kind is EnumBoundaryKind.NATIVE
        assert record.compacted_context_sha256 == digest("Fresh native summary.")


@pytest.mark.parametrize(
    ("output", "reason"),
    [
        (
            ModelAdapterOutput(
                compacted_context="unreadable boundary output",
                boundary_kind=EnumBoundaryKind.UNREADABLE,
            ),
            "BOUNDARY_UNREADABLE",
        ),
        (None, "NO_OUTPUT"),
    ],
    ids=["unreadable-boundary", "no-output"],
)
def test_stock_output_unavailable(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    output: ModelAdapterOutput | None,
    reason: str,
) -> None:
    stock = ScriptedAdapter(output)
    result = HandlerCompactionArmRun(premises, {EnumArm.STOCK_RERUN: stock}).handle(
        arm_request
    )
    for case in arm_request.cases:
        record = _records(result, case.case_id)[EnumArm.STOCK_RERUN]
        assert record.status is EnumArmStatus.UNAVAILABLE
        assert record.reason == reason
        assert record.scorable is False
        assert record.compacted_context_sha256 is None
    assert stock.call_count == 2


@pytest.mark.parametrize(
    ("supplied", "manual", "keeps_transcript", "failing_field", "expected"),
    [
        (False, None, None, "manual_compaction", EnumPremise.UNVERIFIED),
        (True, False, True, "manual_compaction", EnumPremise.FAIL),
        (True, True, False, "fork_keeps_transcript", EnumPremise.FAIL),
        (True, None, True, "manual_compaction", EnumPremise.UNVERIFIED),
        (True, True, None, "fork_keeps_transcript", EnumPremise.UNVERIFIED),
    ],
    ids=[
        "no-premises",
        "manual-rejected",
        "transcript-lost",
        "manual-unknown",
        "transcript-unknown",
    ],
)
def test_refusal_not_hidden(
    arm_request: ModelArmRunRequest,
    stock_adapter: ScriptedAdapter,
    mod_adapter: ScriptedAdapter,
    supplied: bool,
    manual: bool | None,
    keeps_transcript: bool | None,
    failing_field: str,
    expected: EnumPremise,
) -> None:
    premises = ScriptedPremises(manual, keeps_transcript) if supplied else None
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod_adapter}
    ).handle(arm_request)
    _assert_refusal_branch(result)
    assert getattr(result.premises, failing_field) is expected
    assert getattr(result.premises, failing_field) is not EnumPremise.PASS
    assert result.premises.passed is False
    if premises is None:
        assert result.premises.manual_compaction is EnumPremise.UNVERIFIED
        assert result.premises.fork_keeps_transcript is EnumPremise.UNVERIFIED
    else:
        assert premises.manual_calls + premises.transcript_calls > 0
    assert stock_adapter.call_count == 0
    for case in arm_request.cases:
        records = _records(result, case.case_id)
        stock = records[EnumArm.STOCK_RERUN]
        assert stock.status is EnumArmStatus.REFUSED
        assert stock.scorable is False
        assert stock.reason is not None
        assert stock.reason.startswith("PREMISE")
        recorded = records[EnumArm.STOCK_RECORDED]
        assert recorded.status is EnumArmStatus.RECORDED
        assert case.recorded_summary is not None
        assert recorded.compacted_context_sha256 == digest(case.recorded_summary)
        text = (
            _record_directory(arm_request, recorded) / f"attempt-{recorded.attempt}.txt"
        )
        assert text.read_text() == case.recorded_summary


def test_passing_premises_select_stock_rerun(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
    mod_adapter: ScriptedAdapter,
) -> None:
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod_adapter}
    ).handle(arm_request)
    assert result.premises.manual_compaction is EnumPremise.PASS
    assert result.premises.fork_keeps_transcript is EnumPremise.PASS
    assert premises.manual_calls > 0
    assert premises.transcript_calls > 0
    assert result.stock_rerun_refused is False
    assert result.stock_branch is EnumStockBranch.RERUN
    assert result.report_label is None
    assert result.refusal_statement is None
    assert result.planted_stratum_safeguard is None
    for case in arm_request.cases:
        assert (
            _records(result, case.case_id)[EnumArm.STOCK_RERUN].status
            is EnumArmStatus.RECORDED
        )
    assert stock_adapter.call_count == 2


def test_missing_stock_adapter_selects_explicit_refusal_branch(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    mod_adapter: ScriptedAdapter,
) -> None:
    result = HandlerCompactionArmRun(premises, {EnumArm.MOD: mod_adapter}).handle(
        arm_request
    )
    _assert_refusal_branch(result)
    for case in arm_request.cases:
        records = _records(result, case.case_id)
        stock = records[EnumArm.STOCK_RERUN]
        assert stock.status is EnumArmStatus.REFUSED
        assert stock.reason == "STOCK_ADAPTER_UNAVAILABLE"
        assert stock.scorable is False
        assert records[EnumArm.STOCK_RECORDED].status is EnumArmStatus.RECORDED


def test_mod_fallback_counts_native_replies_as_recorded(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
) -> None:
    native_case, mod_case = arm_request.cases
    mod = ScriptedAdapter(
        ModelAdapterOutput(compacted_context="mod", reply_source=EnumReplySource.MOD),
        by_case={
            native_case.case_id: ModelAdapterOutput(
                compacted_context="native fallback", reply_source=EnumReplySource.NATIVE
            )
        },
    )
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod}
    ).handle(arm_request)
    assert result.mod_fallback_count == 1
    assert result.mod_recorded_count == 2
    assert result.mod_fallback_rate == 0.5
    native = _records(result, native_case.case_id)[EnumArm.MOD]
    assert native.status is EnumArmStatus.RECORDED
    assert native.scorable is True
    assert native.reply_source is EnumReplySource.NATIVE
    assert native.compacted_context_sha256 == digest("native fallback")
    assert (
        _records(result, mod_case.case_id)[EnumArm.MOD].reply_source
        is EnumReplySource.MOD
    )


def test_missing_mod_adapter_is_refused_without_a_fallback_rate(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
) -> None:
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter}
    ).handle(arm_request)
    for case in arm_request.cases:
        record = _records(result, case.case_id)[EnumArm.MOD]
        assert record.status is EnumArmStatus.REFUSED
        assert record.reason == "MOD_ADAPTER_UNAVAILABLE"
        assert record.scorable is False
    assert result.mod_fallback_count == 0
    assert result.mod_recorded_count == 0
    assert result.mod_fallback_rate is None


@pytest.mark.parametrize("path_field", ["prefix_path", "ledger_path"])
@pytest.mark.parametrize("symlink", [False, True], ids=["outside", "symlink-outside"])
def test_live_read_refused_before_any_fork_is_created(
    tmp_path: Path,
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
    mod_adapter: ScriptedAdapter,
    path_field: str,
    symlink: bool,
) -> None:
    assert issubclass(LiveReadRefusedError, ValueError)
    outside = tmp_path / "live-session-file.jsonl"
    outside.write_text("live session data must never be read")
    source = outside
    if symlink:
        source = Path(arm_request.replay_root) / "escape.jsonl"
        source.symlink_to(outside)
    # The invalid case is last: validating only as cases are run would already fork case-a.
    valid, invalid = arm_request.cases
    request = arm_request.model_copy(
        update={"cases": (valid, invalid.model_copy(update={path_field: str(source)}))}
    )
    with pytest.raises(LiveReadRefusedError):
        HandlerCompactionArmRun(
            premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod_adapter}
        ).handle(request)
    assert not (Path(request.run_root) / "forks").exists()
    assert stock_adapter.call_count == mod_adapter.call_count == 0
    assert premises.manual_calls == premises.transcript_calls == 0


def test_controls_and_recorded_use_case_contexts_without_adapter_calls(
    tmp_path: Path,
) -> None:
    request = make_request(
        tmp_path,
        arms=(EnumArm.ORACLE, EnumArm.EMPTY, EnumArm.INVERTED, EnumArm.STOCK_RECORDED),
    )
    unverified, verified = request.cases
    request = request.model_copy(
        update={
            "cases": (
                unverified.model_copy(update={"recorded_summary": None}),
                verified,
            )
        }
    )
    spies = {
        arm: ScriptedAdapter(ModelAdapterOutput(compacted_context="must not be called"))
        for arm in EnumArm
    }
    result = HandlerCompactionArmRun(ScriptedPremises(), spies).handle(request)
    assert result.removed_cases == ()
    assert len(result.records) == 8
    assert all(adapter.call_count == 0 for adapter in spies.values())
    for case in request.cases:
        records = _records(result, case.case_id)
        for arm, context in (
            (EnumArm.ORACLE, case.oracle_context),
            (EnumArm.EMPTY, ""),
            (EnumArm.INVERTED, case.inverted_context),
        ):
            record = records[arm]
            assert record.status is EnumArmStatus.RECORDED
            assert record.scorable is True
            assert record.track is EnumArmTrack.CONTROL
            assert record.compacted_context_sha256 == digest(context)
            path = _record_directory(request, record) / f"attempt-{record.attempt}.txt"
            assert path.read_text() == context
        recorded = records[EnumArm.STOCK_RECORDED]
        assert recorded.track is EnumArmTrack.STOCK
        if case.recorded_summary is None:
            assert recorded.status is EnumArmStatus.UNAVAILABLE
            assert recorded.reason == "SUMMARY_UNVERIFIED"
            assert recorded.scorable is False
            assert recorded.compacted_context_sha256 is None
        else:
            assert recorded.status is EnumArmStatus.RECORDED
            assert recorded.compacted_context_sha256 == digest(case.recorded_summary)
            path = (
                _record_directory(request, recorded) / f"attempt-{recorded.attempt}.txt"
            )
            assert path.read_text() == case.recorded_summary


def test_adapter_context_text_and_usage_are_recorded(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
    mod_adapter: ScriptedAdapter,
) -> None:
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod_adapter}
    ).handle(arm_request)
    for case in arm_request.cases:
        records = _records(result, case.case_id)
        for arm, adapter in (
            (EnumArm.STOCK_RERUN, stock_adapter),
            (EnumArm.MOD, mod_adapter),
        ):
            output = adapter.output
            assert output is not None
            assert output.compacted_context is not None
            record = records[arm]
            assert record.status is EnumArmStatus.RECORDED
            assert record.input_tokens == output.input_tokens
            assert record.output_tokens == output.output_tokens
            assert record.compacted_context_sha256 == digest(output.compacted_context)
            assert record.attempt == 1
            path = _record_directory(arm_request, record) / "attempt-1.txt"
            assert path.read_text() == output.compacted_context


def test_track_isolation(
    arm_request: ModelArmRunRequest,
    premises: ScriptedPremises,
    stock_adapter: ScriptedAdapter,
    mod_adapter: ScriptedAdapter,
) -> None:
    result = HandlerCompactionArmRun(
        premises, {EnumArm.STOCK_RERUN: stock_adapter, EnumArm.MOD: mod_adapter}
    ).handle(arm_request)
    assert stock_adapter.call_count == mod_adapter.call_count == 2
    for case in arm_request.cases:
        records = _records(result, case.case_id)
        assert records[EnumArm.STOCK_RERUN].track is EnumArmTrack.STOCK
        assert records[EnumArm.STOCK_RECORDED].track is EnumArmTrack.STOCK
        assert records[EnumArm.MOD].track is EnumArmTrack.MOD
        stock = next(
            fork for fork in stock_adapter.calls if fork.case_id == case.case_id
        )
        mod = next(fork for fork in mod_adapter.calls if fork.case_id == case.case_id)
        assert isinstance(stock, ModelFork)
        assert isinstance(mod, ModelFork)
        assert stock.arm is EnumArm.STOCK_RERUN
        assert stock.track is EnumArmTrack.STOCK
        assert mod.arm is EnumArm.MOD
        assert mod.track is EnumArmTrack.MOD
        assert mod.config_dir != stock.config_dir
        assert mod.root != stock.root
        assert records[EnumArm.STOCK_RERUN].fork_root == stock.root
        assert records[EnumArm.MOD].fork_root == mod.root
