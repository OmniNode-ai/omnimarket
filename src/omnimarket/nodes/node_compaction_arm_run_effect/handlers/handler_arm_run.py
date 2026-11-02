# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fork at one pinned boundary; retain refusal, contamination and native fallback evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from omnimarket.nodes.node_compaction_arm_run_effect.handlers.workspace import (
    HandlerForkWorkspace,
    ModelArmWorkspace,
    digest,
    sha,
    validate_inputs,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    Arm,
    ModelArmRecord,
    ModelArmRunRequest,
    ModelArmRunResult,
    ModelCompactionObservation,
    ModelPremiseEvidence,
    ModelUsage,
)
from omnimarket.nodes.node_compaction_arm_run_effect.protocols.protocol_arm_adapter import (
    ProtocolArmAdapter,
)


class HandlerCompactionArmRun:
    def __init__(
        self,
        *,
        workspace: HandlerForkWorkspace,
        stock: ProtocolArmAdapter,
        mod: ProtocolArmAdapter,
    ) -> None:
        self._workspace, self._stock, self._mod = workspace, stock, mod

    def handle(self, request: ModelArmRunRequest) -> ModelArmRunResult:
        validate_inputs(request)
        records: list[ModelArmRecord] = []
        arms = list(request.arms)
        premise = request.premise
        refused = not premise.accepted
        if refused and "stock_rerun" in arms and "stock_recorded" not in arms:
            arms.append("stock_recorded")
        for repeat in range(request.repeats):
            for arm in arms:
                workspace = self._workspace.fork(request, arm, repeat)
                if arm == "stock_rerun" and refused:
                    observation = ModelCompactionObservation(
                        reply="refused", evidence=premise.evidence
                    )
                else:
                    try:
                        observation = self._compact(request, workspace, arm)
                    except Exception as exc:
                        # An adapter exception is unavailable infrastructure, never
                        # a scored giving-up or a silently substituted stock arm.
                        observation = ModelCompactionObservation(
                            reply="unavailable",
                            evidence=f"adapter error: {type(exc).__name__}",
                        )
                if arm == "stock_rerun" and (
                    observation.reply == "refused"
                    or not observation.transcript_retained
                ):
                    refused = True
                    premise = ModelPremiseEvidence(
                        manual_compaction=False,
                        transcript_retained=observation.transcript_retained,
                        evidence=observation.evidence,
                    )
                    if "stock_recorded" not in arms:
                        arms.append("stock_recorded")
                status, reason = self._status(arm, observation, workspace)
                context = observation.context if status == "OK" else None
                record = ModelArmRecord(
                    case_key=request.case.case_key,
                    arm=arm,
                    repeat_index=repeat,
                    status=status,
                    reason=reason,
                    context=context,
                    context_hash=sha(context.encode()) if context is not None else None,
                    initial_state_hash=workspace.initial_state_hash,
                    prefix_hash=workspace.prefix_hash,
                    ledger_hash=workspace.ledger_hash,
                    compaction_input_hash=digest(
                        [workspace.prefix_hash, workspace.ledger_hash]
                    ),
                    native_fallback=arm == "mod" and observation.reply == "native",
                    usage=observation.usage,
                    evidence=observation.evidence,
                    custom_boundary_evidence=observation.custom_boundary_evidence,
                    package_hash=observation.package_hash,
                    artifact_path=str(workspace.root / "record.json"),
                    adapter_artifacts=observation.adapter_artifacts,
                )
                # Exclusive creation: retries never replace another observation.
                with Path(record.artifact_path).open("x", encoding="utf-8") as file:
                    file.write(record.model_dump_json(indent=2) + "\n")
                records.append(record)
        # Hashes are checked across ALL cells, including refused and invalid ones.
        if (
            len({(r.initial_state_hash, r.prefix_hash, r.ledger_hash) for r in records})
            != 1
        ):
            raise ValueError("arms have unequal initial prefix or ledger state")
        attempts = sum(r.arm == "mod" for r in records)
        fallbacks = sum(r.native_fallback for r in records)
        result = ModelArmRunResult(
            correlation_id=request.correlation_id,
            case_key=request.case.case_key,
            premise=premise,
            records=tuple(records),
            stock_comparison="stock_recorded" if refused else "stock_rerun",
            comparison_label="RECORDED_STOCK" if refused else "FRESH_STOCK",
            planted_safeguard="NOT_ESTABLISHED" if refused else "AVAILABLE",
            aa_control="answerer_only" if refused else "compactor",
            mod_attempts=attempts,
            mod_fallbacks=fallbacks,
            mod_fallback_rate=fallbacks / attempts if attempts else 0.0,
            report_path=str(Path(records[0].artifact_path).parent / "stage.json"),
        )
        with Path(result.report_path).open("x", encoding="utf-8") as file:
            file.write(result.model_dump_json(indent=2) + "\n")
        return result

    @staticmethod
    def _status(
        arm: Arm, observation: ModelCompactionObservation, workspace: ModelArmWorkspace
    ) -> tuple[Literal["OK", "INVALID", "REFUSED", "UNAVAILABLE"], str | None]:
        if not workspace.unchanged():
            return "INVALID", "adapter changed frozen prefix or ledger"
        if arm == "stock_rerun" and (
            observation.custom_boundary_evidence or observation.reply == "installed"
        ):
            return "INVALID", "custom-boundary evidence in stock_rerun"
        if observation.reply == "refused" or not observation.transcript_retained:
            return "REFUSED", observation.evidence
        if observation.reply == "unavailable":
            return "UNAVAILABLE", observation.evidence
        if observation.context is None:
            return "UNAVAILABLE", "no compacted context"
        return "OK", None

    def _compact(
        self, request: ModelArmRunRequest, workspace: ModelArmWorkspace, arm: Arm
    ) -> ModelCompactionObservation:
        if arm == "stock_rerun":
            return self._stock.compact(request, workspace)
        if arm == "mod":
            return self._mod.compact(request, workspace)
        if arm == "stock_recorded":
            return ModelCompactionObservation(
                reply="native",
                context=request.recorded_summary,
                evidence="pinned native transcript summary",
                usage=ModelUsage(
                    input_tokens=request.case.pre_tokens,
                    post_tokens=request.case.post_tokens,
                    measurement="estimated",
                ),
            )
        if arm == "empty":
            context = ""
        else:
            if request.ground_truth is None:
                return ModelCompactionObservation(
                    reply="refused", evidence="control requires ground truth"
                )
            context = "\n\n".join(
                request.predecessors.get(
                    item.key, "NOT (" + "\n".join(item.required_spans) + ")"
                )
                if arm == "inverted" and item.kind == "RULING"
                else "\n".join(item.required_spans)
                for item in request.ground_truth.items
            )
        return ModelCompactionObservation(
            reply="installed",
            context=context,
            evidence="deterministic ground-truth control",
        )
