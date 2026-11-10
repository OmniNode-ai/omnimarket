# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one pull request head through the validators as one workflow (OMN-23078).

Order: read effect (the change) -> choose validators from their contracts -> read effect (each
chosen validator's typed input) -> the validator compute nodes -> the verdict reducer -> emit
effect (the bus trace) -> gate effect (the check conclusion). A read that fails posts a failing
gate, never a passing one; a validator that raises is an errored outcome and fails the verdict.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.models.model_validator_verdict import (
    EnumPrVerdict,
    ModelValidatorOutcome,
    ModelVerdictFoldRequest,
)
from omnimarket.nodes.node_validator_verdict_reducer.handlers.handler_validator_verdict_reducer import (
    HandlerValidatorVerdictReducer,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.handlers.validator_selection import (
    choose,
    resolve_handler,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.models.model_validator_workflow import (
    EnumGateConclusion,
    EnumSelection,
    ModelGateDecision,
    ModelValidatorChoice,
    ModelValidatorWorkflowRequest,
    ModelValidatorWorkflowResult,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.overlay import (
    ModelValidatorWorkflowOverlay,
)
from omnimarket.nodes.node_validator_workflow_orchestrator.protocols import (
    ProtocolChangeReader,
    ProtocolCheckGate,
    ProtocolContractSource,
    ProtocolTraceEmitter,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
FAILING_SEVERITIES = frozenset({"FAIL", "ERROR"})


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["topics"])


def _outcome(choice: ModelValidatorChoice, report: object) -> ModelValidatorOutcome:
    findings = getattr(report, "findings", ())
    failing = [f for f in findings if getattr(f, "severity", "") in FAILING_SEVERITIES]
    return ModelValidatorOutcome(
        validator_id=choice.validator_id,
        canonical=True,
        failing_count=len(failing),
        rule_ids=tuple(sorted({str(f.rule_id) for f in failing if f.rule_id})),
    )


class HandlerValidatorWorkflow:
    def __init__(
        self,
        *,
        overlay: ModelValidatorWorkflowOverlay,
        reader: ProtocolChangeReader,
        contracts: ProtocolContractSource,
        reducer: HandlerValidatorVerdictReducer,
        emitter: ProtocolTraceEmitter,
        gate: ProtocolCheckGate,
    ) -> None:
        self._overlay = overlay
        self._reader = reader
        self._contracts = contracts
        self._reducer = reducer
        self._emitter = emitter
        self._gate = gate
        self._topics = load_topics()

    def _emit(
        self, role: str, request: ModelValidatorWorkflowRequest, **body: object
    ) -> None:
        payload: dict[str, object] = {
            "correlation_id": request.correlation_id,
            "repo": request.repo,
            "pr": request.pr,
            "head": request.head,
            **body,
        }
        self._emitter.emit(self._topics[role], payload)

    def _gate_post(
        self, request: ModelValidatorWorkflowRequest, passed: bool, summary: str
    ) -> ModelGateDecision:
        decision = ModelGateDecision(
            repo=request.repo,
            pr=request.pr,
            head=request.head,
            check_name=self._overlay.gate.check_name,
            conclusion=EnumGateConclusion.SUCCESS
            if passed
            else EnumGateConclusion.FAILURE,
            summary=summary,
        )
        self._gate.post(decision)
        return decision

    def handle(
        self, request: ModelValidatorWorkflowRequest
    ) -> ModelValidatorWorkflowResult:
        self._emit("workflow_started", request)
        try:
            change = self._reader.read_change(request)
        except Exception as exc:
            failure = f"read-failed: {type(exc).__name__}: {exc}"
            self._emit("workflow_failed", request, failure=failure)
            gate = self._gate_post(request, False, failure)
            return ModelValidatorWorkflowResult(
                correlation_id=request.correlation_id,
                status="failed",
                failure=failure,
                gate=gate,
            )
        choices: list[ModelValidatorChoice] = []
        outcomes: list[ModelValidatorOutcome] = []
        for entry in self._overlay.validators:
            choice, validation = choose(
                entry, change.changed_paths, self._contracts, self._overlay.canonical
            )
            choices.append(choice)
            if choice.selection is not EnumSelection.SELECTED:
                self._emit(
                    "validator_skipped",
                    request,
                    validator_id=choice.validator_id,
                    selection=choice.selection.value,
                    reason=choice.reason,
                )
                if choice.selection is EnumSelection.CONTRACT_UNREADABLE:
                    outcomes.append(
                        ModelValidatorOutcome(
                            validator_id=choice.validator_id,
                            canonical=False,
                            failing_count=0,
                            error=choice.reason,
                        )
                    )
                continue
            try:
                validator_input = self._reader.validator_input(
                    request, entry.input_kind, validation
                )
                report = resolve_handler(choice.handler_ref).handle(validator_input)
                outcome = _outcome(choice, report)
            except Exception as exc:
                outcome = ModelValidatorOutcome(
                    validator_id=choice.validator_id,
                    canonical=True,
                    failing_count=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            outcomes.append(outcome)
            self._emit(
                "validator_completed",
                request,
                validator_id=outcome.validator_id,
                failing_count=outcome.failing_count,
                rule_ids=list(outcome.rule_ids),
                error=outcome.error,
            )
        folded = self._reducer.handle(
            ModelVerdictFoldRequest(
                correlation_id=request.correlation_id,
                repo=request.repo,
                pr=request.pr,
                head=request.head,
                outcomes=tuple(outcomes),
                ratchet_readings=change.ratchet_readings,
            )
        )
        verdict = folded.verdict
        for refusal in verdict.refused_ratchets:
            self._emit("ratchet_refused", request, **refusal.model_dump(mode="json"))
        self._emit(
            "verdict_folded",
            request,
            verdict=verdict.verdict.value,
            failing_validators=list(verdict.failing_validators),
            errored_validators=list(verdict.errored_validators),
            canonical_validator_count=folded.canonical_validator_count,
        )
        passed = verdict.verdict is EnumPrVerdict.PASS
        summary = (
            "all chosen validators passed"
            if passed
            else "failing="
            + ",".join(verdict.failing_validators + verdict.errored_validators)
            + (
                " refused_ratchets=" + ",".join(r.key for r in verdict.refused_ratchets)
                if verdict.refused_ratchets
                else ""
            )
        )
        gate = self._gate_post(request, passed, summary)
        self._emit("workflow_completed", request, verdict=verdict.verdict.value)
        return ModelValidatorWorkflowResult(
            correlation_id=request.correlation_id,
            status="completed",
            choices=tuple(choices),
            outcomes=tuple(outcomes),
            verdict=verdict,
            moves=folded.moves,
            canonical_validator_count=folded.canonical_validator_count,
            gate=gate,
        )
