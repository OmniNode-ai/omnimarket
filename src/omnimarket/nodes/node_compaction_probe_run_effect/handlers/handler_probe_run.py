# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answerer, judge and the judge's calibration, every call recorded under its key.

HandlerJudgeCalibration runs the oracle, empty and inverted control cases through the
fixed answering model and the different judge model and reports the judge's Wilson
lower bounds. HandlerCompactionProbeRun answers and judges arm cases, and refuses to
start without a passing calibration report for the same manifest and models.

A call is made only in RECORD mode and only for a key with no valid recording. REPLAY
holds no caller and reads recordings alone; a key with no valid recording scores
``UNAVAILABLE``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from omnimarket.nodes.node_compaction_probe_run_effect.handlers.model_caller import (
    ProtocolModelCaller,
)
from omnimarket.nodes.node_compaction_probe_run_effect.handlers.recording_store import (
    ModelAttempt,
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
    ModelControlTally,
    ModelProbeCase,
    ModelProbeRunRequest,
    ModelProbeRunResult,
    ModelScoredAnswer,
    _ModelRunBase,
)

__all__ = ["HandlerCompactionProbeRun", "HandlerJudgeCalibration"]

ORACLE_MIN_RATE = 0.95
LOWER_BOUND_MIN = 0.90
EMPTY_RECALL_MAX = 0.05
_EXPECTED = {
    EnumControl.ORACLE: EnumProbeVerdict.RECALLED,
    EnumControl.EMPTY: EnumProbeVerdict.ABSENT,
    EnumControl.INVERTED: EnumProbeVerdict.CONTRADICTED,
}
_LABELS = {
    "RECALLED": EnumProbeVerdict.RECALLED,
    "ABSENT": EnumProbeVerdict.ABSENT,
    "CONTRADICTED": EnumProbeVerdict.CONTRADICTED,
}
JUDGE_INSTRUCTION = (
    "You judge one answer against an operator's recorded decision. Reply with exactly "
    "one word: RECALLED if the answer states the decision, CONTRADICTED if it states "
    "the superseded or opposite decision, ABSENT if it gives no answer or says it does "
    "not know."
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def answer_prompt(case: ModelProbeCase) -> str:
    return f"{case.context}\n\n{case.probe_text}" if case.context else case.probe_text


def judge_prompt(case: ModelProbeCase, answer: str) -> str:
    superseded = (
        f"\nSUPERSEDED: {case.predecessor_text}" if case.predecessor_text else ""
    )
    return (
        f"{JUDGE_INSTRUCTION}\nQUESTION: {case.question}\nDECISION: {case.quote}"
        f"{superseded}\nANSWER:{answer}"
    )


def parse_verdict(text: str) -> EnumProbeVerdict | None:
    words = text.strip().split()
    if not words:
        return None
    return _LABELS.get(words[0].strip(".,:;!\"'`*").upper())


class _Recorder:
    def __init__(
        self, request: _ModelRunBase, caller: ProtocolModelCaller | None
    ) -> None:
        self._store = RecordingStore(request.recording_root)
        self._caller = caller if request.mode is EnumRunMode.RECORD else None
        self._digest = request.manifest.digest()
        self._settings = request.settings

    def _key(
        self, model_id: str, prompt_hash: str, context: str, repeat: int
    ) -> RecordingKey:
        return RecordingKey(
            manifest_digest=self._digest,
            served_model_id=model_id,
            prompt_hash=prompt_hash,
            context_hash=_sha(context),
            decoding=self._settings.canonical(),
            repeat_index=repeat,
        )

    def obtain(
        self,
        model_id: str,
        prompt: str,
        prompt_hash: str,
        context: str,
        repeat: int,
        *,
        valid: Callable[[str], bool] | None = None,
    ) -> ModelAttempt | None:
        """The latest valid attempt for the key, calling the model only to fill a gap."""
        key = self._key(model_id, prompt_hash, context, repeat)
        existing = self._store.latest_valid(key)
        if existing is not None:
            return existing
        if self._caller is None:
            return None
        try:
            output = self._caller.call(model_id, prompt, self._settings)
        except Exception:
            output = None
        usable = output is not None and output.strip() != ""
        if usable and output is not None and valid is not None:
            usable = valid(output)
        attempt = self._store.append(key, ok=usable, output=output)
        return attempt if attempt.status == "OK" else None

    def answer(
        self, model_id: str, case: ModelProbeCase, repeat: int
    ) -> ModelAttempt | None:
        return self.obtain(
            model_id,
            answer_prompt(case),
            _sha(case.probe_text),
            case.context,
            repeat,
        )

    def judge(
        self, model_id: str, case: ModelProbeCase, answer: str, repeat: int
    ) -> tuple[EnumProbeVerdict, int | None]:
        prompt = judge_prompt(case, answer)
        attempt = self.obtain(
            model_id,
            prompt,
            _sha(prompt),
            answer,
            repeat,
            valid=lambda text: parse_verdict(text) is not None,
        )
        if attempt is None or attempt.output is None:
            return EnumProbeVerdict.UNAVAILABLE, None
        verdict = parse_verdict(attempt.output)
        if verdict is None:
            return EnumProbeVerdict.UNAVAILABLE, attempt.attempt
        return verdict, attempt.attempt


def _score(
    request: _ModelRunBase,
    caller: ProtocolModelCaller | None,
    cases: tuple[ModelProbeCase, ...],
) -> tuple[ModelScoredAnswer, ...]:
    recorder = _Recorder(request, caller)
    scored: list[ModelScoredAnswer] = []
    for case in cases:
        for repeat in range(request.repeats):
            answer = recorder.answer(request.answerer_model_id, case, repeat)
            if answer is None or answer.output is None:
                scored.append(
                    ModelScoredAnswer(
                        case_id=case.case_id,
                        repeat_index=repeat,
                        answer=None,
                        answer_attempt=None,
                        verdict=EnumProbeVerdict.UNAVAILABLE,
                        judge_attempt=None,
                    )
                )
                continue
            verdict, judge_attempt = recorder.judge(
                request.judge_model_id, case, answer.output, repeat
            )
            scored.append(
                ModelScoredAnswer(
                    case_id=case.case_id,
                    repeat_index=repeat,
                    answer=answer.output,
                    answer_attempt=answer.attempt,
                    verdict=verdict,
                    judge_attempt=judge_attempt,
                )
            )
    return tuple(scored)


def _tally(
    control: EnumControl,
    cases: tuple[ModelProbeCase, ...],
    scored: tuple[ModelScoredAnswer, ...],
) -> ModelControlTally:
    ids = {case.case_id for case in cases if case.control is control}
    rows = [row for row in scored if row.case_id in ids]
    expected = _EXPECTED[control]

    def count(verdict: EnumProbeVerdict) -> int:
        return sum(1 for row in rows if row.verdict is verdict)

    hits = count(expected)
    return ModelControlTally(
        control=control,
        expected=expected,
        n=len(rows),
        recalled=count(EnumProbeVerdict.RECALLED),
        absent=count(EnumProbeVerdict.ABSENT),
        contradicted=count(EnumProbeVerdict.CONTRADICTED),
        unavailable=count(EnumProbeVerdict.UNAVAILABLE),
        rate=hits / len(rows) if rows else 0.0,
        wilson_lower=wilson_lower(hits, len(rows)),
    )


class HandlerJudgeCalibration:
    """Judge calibration on controls 2 to 4 of the measurement plan."""

    def __init__(self, caller: ProtocolModelCaller | None = None) -> None:
        self._caller = caller

    def handle(self, request: ModelCalibrationRequest) -> ModelCalibrationReport:
        controls = tuple(c for c in request.cases if c.control in _EXPECTED)
        scored = _score(request, self._caller, controls)
        oracle = _tally(EnumControl.ORACLE, controls, scored)
        empty = _tally(EnumControl.EMPTY, controls, scored)
        inverted = _tally(EnumControl.INVERTED, controls, scored)
        failures: list[str] = []
        for name, tally in (
            ("oracle", oracle),
            ("empty", empty),
            ("inverted", inverted),
        ):
            if tally.n == 0:
                failures.append(f"{name}: no cases")
        if oracle.n:
            if oracle.rate < ORACLE_MIN_RATE:
                failures.append(
                    f"oracle: recall {oracle.rate:.3f} below {ORACLE_MIN_RATE}"
                )
            if oracle.wilson_lower < LOWER_BOUND_MIN:
                failures.append(
                    f"oracle: Wilson lower bound {oracle.wilson_lower:.3f} below {LOWER_BOUND_MIN}"
                )
            if oracle.contradicted:
                failures.append(
                    f"oracle: {oracle.contradicted} answers read CONTRADICTED"
                )
        if inverted.n and inverted.wilson_lower < LOWER_BOUND_MIN:
            failures.append(
                f"inverted: Wilson lower bound {inverted.wilson_lower:.3f} below {LOWER_BOUND_MIN}"
            )
        if empty.n and empty.recalled / empty.n > EMPTY_RECALL_MAX:
            failures.append(
                f"empty: recall {empty.recalled / empty.n:.3f} above {EMPTY_RECALL_MAX}"
            )
        if empty.n and empty.unavailable:
            failures.append(f"empty: {empty.unavailable} answers unavailable")
        return ModelCalibrationReport(
            correlation_id=request.correlation_id,
            manifest_digest=request.manifest.digest(),
            answerer_model_id=request.answerer_model_id,
            judge_model_id=request.judge_model_id,
            oracle=oracle,
            empty=empty,
            inverted=inverted,
            failures=tuple(failures),
            passed=not failures,
        )


class HandlerCompactionProbeRun:
    """Answer and judge arm cases; no scoring starts without a passing calibration."""

    def __init__(self, caller: ProtocolModelCaller | None = None) -> None:
        self._caller = caller

    def handle(self, request: ModelProbeRunRequest) -> ModelProbeRunResult:
        report = request.calibration_report
        reason = self._refusal(request, report)
        if reason is not None:
            return ModelProbeRunResult(
                correlation_id=request.correlation_id,
                status=EnumRunStatus.CALIBRATION_REQUIRED,
                reason=reason,
            )
        return ModelProbeRunResult(
            correlation_id=request.correlation_id,
            status=EnumRunStatus.COMPLETED,
            results=_score(request, self._caller, request.cases),
        )

    @staticmethod
    def _refusal(
        request: ModelProbeRunRequest, report: ModelCalibrationReport | None
    ) -> str | None:
        if report is None:
            return "no calibration report"
        if not report.passed:
            return "calibration report did not pass: " + "; ".join(report.failures)
        if (
            report.manifest_digest != request.manifest.digest()
            or report.judge_model_id != request.judge_model_id
            or report.answerer_model_id != request.answerer_model_id
        ):
            return "calibration report is for another manifest or model pair"
        return None
