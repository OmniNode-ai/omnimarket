# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CM.8: recorded answerer and judge calls, the judge's calibration, replay with no call."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_compaction_probe_run_effect.handlers.handler_probe_run import (
    HandlerCompactionProbeRun,
    HandlerJudgeCalibration,
)
from omnimarket.nodes.node_compaction_probe_run_effect.handlers.recording_store import (
    RecordingKey,
    RecordingStore,
    wilson_lower,
)
from omnimarket.nodes.node_compaction_probe_run_effect.models.model_probe_run import (
    EnumControl,
    EnumProbeVerdict,
    EnumRunMode,
    EnumRunStatus,
    ModelCalibrationReport,
    ModelCalibrationRequest,
    ModelProbeRunRequest,
)

from .conftest import ANSWERER, JUDGE, SETTINGS, cases, manifest, scripted_caller

pytestmark = pytest.mark.unit


def _calibration_request(root: Path, mode: EnumRunMode) -> ModelCalibrationRequest:
    return ModelCalibrationRequest(
        correlation_id="cal-1",
        mode=mode,
        recording_root=str(root),
        manifest=manifest(),
        answerer_model_id=ANSWERER,
        judge_model_id=JUDGE,
        settings=SETTINGS,
        repeats=3,
        cases=cases(),
    )


def _run_request(
    root: Path, mode: EnumRunMode, report: ModelCalibrationReport | None
) -> ModelProbeRunRequest:
    return ModelProbeRunRequest(
        correlation_id="run-1",
        mode=mode,
        recording_root=str(root),
        manifest=manifest(),
        answerer_model_id=ANSWERER,
        judge_model_id=JUDGE,
        settings=SETTINGS,
        repeats=3,
        cases=tuple(c for c in cases() if c.control is EnumControl.ORACLE),
        calibration_report=report,
    )


def test_judge_calibration_passes_on_good_judge(tmp_path: Path) -> None:
    caller = scripted_caller()
    report = HandlerJudgeCalibration(caller).handle(
        _calibration_request(tmp_path, EnumRunMode.RECORD)
    )
    assert report.oracle.n == 42
    assert report.oracle.wilson_lower >= 0.90
    assert report.inverted.wilson_lower >= 0.90
    assert report.passed is True
    assert report.failures == ()


def test_judge_calibration_fails_below_either_bound(tmp_path: Path) -> None:
    caller = scripted_caller(judge_wrong_on_oracle=6)
    report = HandlerJudgeCalibration(caller).handle(
        _calibration_request(tmp_path, EnumRunMode.RECORD)
    )
    assert report.oracle.wilson_lower < 0.90
    assert report.passed is False
    assert any("oracle" in failure for failure in report.failures)


def test_judge_calibration_unavailable_answers_never_pass(tmp_path: Path) -> None:
    caller = scripted_caller(answerer_ok=False)
    report = HandlerJudgeCalibration(caller).handle(
        _calibration_request(tmp_path, EnumRunMode.RECORD)
    )
    assert report.oracle.unavailable == report.oracle.n
    assert report.passed is False


def test_judge_calibration_requires_all_three_controls(tmp_path: Path) -> None:
    request = _calibration_request(tmp_path, EnumRunMode.RECORD)
    only_oracle = request.model_copy(
        update={
            "cases": tuple(c for c in request.cases if c.control is EnumControl.ORACLE)
        }
    )
    report = HandlerJudgeCalibration(scripted_caller()).handle(only_oracle)
    assert report.passed is False
    assert any("inverted" in failure for failure in report.failures)


def test_judge_calibration_gates_scoring(tmp_path: Path) -> None:
    caller = scripted_caller()
    refused = HandlerCompactionProbeRun(caller).handle(
        _run_request(tmp_path, EnumRunMode.RECORD, None)
    )
    assert refused.status is EnumRunStatus.CALIBRATION_REQUIRED
    assert refused.results == ()
    assert caller.calls == []
    bad = HandlerJudgeCalibration(scripted_caller(judge_wrong_on_oracle=6)).handle(
        _calibration_request(tmp_path / "bad", EnumRunMode.RECORD)
    )
    refused = HandlerCompactionProbeRun(caller).handle(
        _run_request(tmp_path, EnumRunMode.RECORD, bad)
    )
    assert refused.status is EnumRunStatus.CALIBRATION_REQUIRED
    assert caller.calls == []
    good = HandlerJudgeCalibration(scripted_caller()).handle(
        _calibration_request(tmp_path / "good", EnumRunMode.RECORD)
    )
    ran = HandlerCompactionProbeRun(caller).handle(
        _run_request(tmp_path, EnumRunMode.RECORD, good)
    )
    assert ran.status is EnumRunStatus.COMPLETED
    assert len(ran.results) == 14 * 3
    assert all(r.verdict is EnumProbeVerdict.RECALLED for r in ran.results)


def test_answerer_judge_differ() -> None:
    with pytest.raises(ValidationError, match="differ"):
        ModelCalibrationRequest(
            correlation_id="x",
            mode=EnumRunMode.REPLAY,
            recording_root="/nonexistent",
            manifest=manifest(),
            answerer_model_id=ANSWERER,
            judge_model_id=ANSWERER,
            settings=SETTINGS,
            repeats=3,
            cases=cases(),
        )
    with pytest.raises(ValidationError, match="compactor"):
        ModelCalibrationRequest(
            correlation_id="x",
            mode=EnumRunMode.REPLAY,
            recording_root="/nonexistent",
            manifest=manifest(),
            answerer_model_id=ANSWERER,
            judge_model_id=JUDGE,
            compactor_model_ids=(JUDGE,),
            settings=SETTINGS,
            repeats=3,
            cases=cases(),
        )


def test_answerer_judge_differ_in_report(tmp_path: Path) -> None:
    report = HandlerJudgeCalibration(scripted_caller()).handle(
        _calibration_request(tmp_path, EnumRunMode.RECORD)
    )
    assert report.answerer_model_id == ANSWERER
    assert report.judge_model_id == JUDGE
    assert report.answerer_model_id != report.judge_model_id


def test_replay_no_network(tmp_path: Path, no_network: list[str]) -> None:
    recorded = HandlerJudgeCalibration(scripted_caller()).handle(
        _calibration_request(tmp_path, EnumRunMode.RECORD)
    )
    replayed = HandlerJudgeCalibration(None).handle(
        _calibration_request(tmp_path, EnumRunMode.REPLAY)
    )
    assert replayed.model_dump_json() == recorded.model_dump_json()
    assert no_network == []


def test_replay_no_model_call_even_with_a_caller_present(tmp_path: Path) -> None:
    HandlerJudgeCalibration(scripted_caller()).handle(
        _calibration_request(tmp_path, EnumRunMode.RECORD)
    )
    watcher = scripted_caller()
    HandlerJudgeCalibration(watcher).handle(
        _calibration_request(tmp_path, EnumRunMode.REPLAY)
    )
    assert watcher.calls == []


def test_replay_missing_recording_is_unavailable_not_a_call(tmp_path: Path) -> None:
    watcher = scripted_caller()
    report = HandlerJudgeCalibration(watcher).handle(
        _calibration_request(tmp_path / "empty", EnumRunMode.REPLAY)
    )
    assert watcher.calls == []
    assert report.oracle.unavailable == report.oracle.n
    assert report.passed is False


def test_unavailable_answer_is_scored_unavailable(tmp_path: Path) -> None:
    good = HandlerJudgeCalibration(scripted_caller()).handle(
        _calibration_request(tmp_path / "good", EnumRunMode.RECORD)
    )
    ran = HandlerCompactionProbeRun(scripted_caller(answerer_ok=False)).handle(
        _run_request(tmp_path / "run", EnumRunMode.RECORD, good)
    )
    assert {r.verdict for r in ran.results} == {EnumProbeVerdict.UNAVAILABLE}
    assert all(r.answer is None for r in ran.results)


def test_repeat_keys(tmp_path: Path) -> None:
    HandlerJudgeCalibration(scripted_caller()).handle(
        _calibration_request(tmp_path, EnumRunMode.RECORD)
    )
    digest = manifest().digest()
    files = sorted((tmp_path / digest).glob("*.jsonl"))
    keys = [json.loads(f.read_text().splitlines()[0])["key"] for f in files]
    answer_keys = [k for k in keys if k["served_model_id"] == ANSWERER]
    # one oracle case: three repeats are three keys that differ only by repeat index
    oracle_prompt = [k for k in answer_keys if k["repeat_index"] in (0, 1, 2)]
    by_context: dict[tuple[str, str], set[int]] = {}
    for k in oracle_prompt:
        by_context.setdefault((k["prompt_hash"], k["context_hash"]), set()).add(
            k["repeat_index"]
        )
    assert any(v == {0, 1, 2} for v in by_context.values())
    assert all(k["manifest_digest"] == digest for k in keys)
    assert len({RecordingKey(**k).digest() for k in keys}) == len(keys)


def test_repeat_keys_change_with_manifest() -> None:
    def make(
        manifest_digest: str = "m1", decoding: str = "d", repeat_index: int = 0
    ) -> RecordingKey:
        return RecordingKey(
            manifest_digest=manifest_digest,
            served_model_id="a",
            prompt_hash="p",
            context_hash="c",
            decoding=decoding,
            repeat_index=repeat_index,
        )

    base = make()
    assert make(repeat_index=1).digest() != base.digest()
    assert make(manifest_digest="m2").digest() != base.digest()
    assert make(decoding="e").digest() != base.digest()


def test_retry_appends(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path)
    key = RecordingKey(
        manifest_digest="m",
        served_model_id=ANSWERER,
        prompt_hash="p",
        context_hash="c",
        decoding="d",
        repeat_index=0,
    )
    first = store.append(key, ok=False, output=None)
    assert store.latest_valid(key) is None
    second = store.append(key, ok=True, output="recovered")
    third = store.append(key, ok=True, output="later")
    attempts = store.attempts(key)
    assert [a.attempt for a in attempts] == [1, 2, 3]
    assert attempts[0] == first
    assert attempts[1] == second
    assert attempts[2] == third
    latest = store.latest_valid(key)
    assert latest is not None
    assert latest.output == "later"
    raw = store.path(key).read_text().splitlines()
    assert len(raw) == 3
    assert json.loads(raw[0])["status"] == "UNAVAILABLE"


def test_retry_appends_through_the_handler(tmp_path: Path) -> None:
    request = _calibration_request(tmp_path, EnumRunMode.RECORD)
    HandlerJudgeCalibration(scripted_caller(answerer_ok=False)).handle(request)
    HandlerJudgeCalibration(scripted_caller()).handle(request)
    digest = manifest().digest()
    lines = [
        json.loads(line)
        for f in (tmp_path / digest).glob("*.jsonl")
        for line in f.read_text().splitlines()
    ]
    assert {a["attempt"] for a in lines} == {1, 2}
    unavailable = [a for a in lines if a["status"] == "UNAVAILABLE"]
    assert unavailable
    assert all(a["attempt"] == 1 for a in unavailable)
    # a recorded valid attempt is reused: a third run makes no call
    watcher = scripted_caller()
    HandlerJudgeCalibration(watcher).handle(request)
    assert watcher.calls == []


def test_wilson_lower_known_values() -> None:
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(36, 36) == pytest.approx(0.9036, abs=1e-3)
    assert wilson_lower(8, 8) < 0.90
    assert wilson_lower(40, 42) < wilson_lower(42, 42)
