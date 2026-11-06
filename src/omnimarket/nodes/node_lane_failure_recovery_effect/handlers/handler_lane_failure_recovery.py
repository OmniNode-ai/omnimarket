# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contribute memory/ownership changes to the one shared attempt-family plan."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorEvent,
    ModelErrorSubject,
)
from omnimarket.models.model_lane_failure import (
    ModelLaneFailureAdmissionRequest,
    ModelLaneFailureMatch,
    ModelLaneFailureOverlay,
    ModelLaneFailureOwnerRequest,
    ModelLaneFailureRequest,
    ModelLaneFailureResult,
    ModelLaneFailureSelector,
    ModelLaneRecoveryContribution,
)
from omnimarket.protocols.protocol_lane_failure import ProtocolLaneFailureGateway


def recovery_key(*parts: str) -> str:
    """Unambiguous natural-key encoding shared with the plan/claim consumer."""
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


class HandlerLaneFailureRecovery:
    def __init__(
        self,
        overlay: ModelLaneFailureOverlay | None = None,
        gateway: ProtocolLaneFailureGateway | None = None,
    ) -> None:
        self._overlay = overlay
        self._gateway = gateway

    def _bind(self) -> None:
        """Resolve deployment dependencies on demand, after runtime wiring."""
        if self._overlay is None or self._gateway is None:
            from ..container.container_lane_failure_recovery import build_dependencies

            configured_overlay, configured_gateway = build_dependencies(
                explicit=self._overlay
            )
            if self._overlay is None:
                self._overlay = configured_overlay
            if self._gateway is None:
                self._gateway = configured_gateway

    @property
    def overlay(self) -> ModelLaneFailureOverlay:
        self._bind()
        assert self._overlay is not None
        return self._overlay

    @property
    def gateway(self) -> ProtocolLaneFailureGateway:
        self._bind()
        assert self._gateway is not None
        return self._gateway

    async def handle(self, request: ModelLaneFailureRequest) -> ModelLaneFailureResult:
        receipt = request.receipt
        # Neither exit 137 nor a retention error is evidence of an OOM kill.
        # Only the configured scope-result selector can choose that row.
        retention = "\n".join((receipt.retention.error, receipt.retention.reading))
        selected = [
            selector
            for selector in self.overlay.selectors
            if (
                selector.signal == "oom"
                and selector.scope_result is not None
                and receipt.guard.end.scope_result == selector.scope_result
            )
            or (
                selector.signal == "retention"
                and selector.error_pattern is not None
                and re.search(selector.error_pattern, retention) is not None
            )
        ]
        matches = []
        for selector in selected:
            # Both signals are processed independently, including on one receipt.
            # Subject/exit/duration/path never become the producer identity.
            normalized = await self.gateway.normalize(
                ModelErrorClassNormalizeRequest(
                    event=ModelErrorEvent(
                        source=self.overlay.source,
                        producer=self.overlay.producer,
                        code=selector.rule_id,
                        text=selector.canonical_text,
                        subject=ModelErrorSubject(
                            lane=receipt.lane,
                            host=receipt.host,
                            ticket=receipt.ticket,
                            repository=receipt.repo.repo if receipt.repo else "",
                            head=receipt.repo.sha if receipt.repo else "",
                            time=receipt.started_at,
                        ),
                    ),
                    facts=self.overlay.normalizer_facts,
                    key_version=self.overlay.normalizer_key_version,
                )
            )
            admission = await self.gateway.admit(
                ModelLaneFailureAdmissionRequest(
                    document=self.overlay.error_rule_document,
                    rule_id=selector.rule_id,
                    class_key=normalized.class_key,
                    key_version=self.overlay.rule_key_version,
                    normalizer_key_version=normalized.key_version,
                    scope_result=receipt.guard.end.scope_result,
                    repository=receipt.repo.repo if receipt.repo else "",
                )
            )
            if (
                admission.rule_id != selector.rule_id
                or admission.class_key != normalized.class_key
            ):
                raise ValueError("core admission replied for a different failure class")
            base = {
                "rule_id": selector.rule_id,
                "class_key": normalized.class_key,
                "key_version": normalized.key_version,
            }
            if not admission.allowed:
                matches.append(
                    ModelLaneFailureMatch(
                        **base, status="refused", reason=admission.reason
                    )
                )
                continue
            if admission.mode != "dispatch":
                matches.append(
                    ModelLaneFailureMatch(
                        **base, status=admission.mode, reason=admission.reason
                    )
                )
                continue
            contribution = await self._contribution(
                request, selector, normalized.class_key
            )
            if contribution is None:
                matches.append(
                    ModelLaneFailureMatch(
                        **base,
                        status="refused",
                        reason="owner or originating repository is unresolved",
                    )
                )
                continue
            # NO launcher here. The shared plan owns the atomic claim and start.
            # Retrying after a lost reply resubmits the same natural key.
            try:
                ack = await self.gateway.contribute(contribution)
            except TimeoutError:
                matches.append(
                    ModelLaneFailureMatch(
                        **base,
                        status="pending",
                        contribution=contribution,
                        reason="shared recovery reply lost; replay the same contribution key",
                    )
                )
                continue
            if ack.key != contribution.key:
                raise ValueError("shared recovery replied for a different claim")
            matches.append(
                ModelLaneFailureMatch(
                    **base,
                    status=ack.status,
                    contribution=contribution,
                    lane=ack.lane,
                )
            )
        return ModelLaneFailureResult(matches=tuple(matches))

    async def _contribution(
        self,
        request: ModelLaneFailureRequest,
        selector: ModelLaneFailureSelector,
        class_key: str,
    ) -> ModelLaneRecoveryContribution | None:
        receipt = request.receipt
        memory = None
        salvage_worktree = None
        salvage_host = None
        target_repo = request.target_repository or (
            receipt.repo.repo if receipt.repo else ""
        )
        operation: Literal["replace", "runner-fix", "escalate"] = "replace"
        # Repositories and memory are inputs to this SAME family, not separate starts.
        key = recovery_key("replacement", request.attempt_family)
        family = request.attempt_family
        if selector.signal == "retention":
            owner = await self.gateway.owner(
                ModelLaneFailureOwnerRequest(
                    blocker=self.overlay.runner_path,
                    # Diagnose the runner component. The failed import names
                    # a missing dependency, which is not ownership evidence.
                    # The full typed receipt stays on the contribution below.
                    report_text=self.overlay.runner_path,
                    owner_map=self.overlay.owner_map,
                )
            )
            target_repo = owner.owner or ""
            operation = "runner-fix"
            # One runner fix for this class across every originating lane.
            key = recovery_key("runner-fix", class_key)
            family = key
        elif request.replacement_index >= self.overlay.max_replacements:
            operation = "escalate"
            key = recovery_key("oom-escalation", request.attempt_family)
        else:
            current = receipt.guard.requested.mem_bytes
            steps = sorted(self.overlay.memory_steps, key=lambda step: step.mem_bytes)
            memory = next(
                (
                    step
                    for step in steps
                    if current < step.mem_bytes <= self.overlay.max_mem_bytes
                ),
                None,
            )
            # Missing measured limit or no larger permitted step cannot start a lane.
            if current <= 0 or memory is None:
                memory = None
                operation = "escalate"
                key = recovery_key("oom-escalation", request.attempt_family)
            workdir = receipt.workdir
            if (
                workdir is not None
                and not isinstance(workdir, str)
                and workdir.disposition == self.overlay.kept_workdir_disposition
            ):
                salvage_worktree = workdir.tree
                salvage_host = receipt.host
        if not target_repo:
            return None
        return ModelLaneRecoveryContribution(
            operation=operation,
            key=key,
            attempt_family=family,
            original_lane=request.original_lane,
            class_key=class_key,
            rule_id=selector.rule_id,
            target_repo=target_repo,
            for_items=request.for_items,
            brief=request.original_brief
            if selector.signal == "oom"
            else self.overlay.runner_report,
            receipt_run_id=receipt.run_id,
            evidence=receipt,
            mem_max=memory.mem_max if memory else None,
            mem_bytes=memory.mem_bytes if memory else None,
            salvage_worktree=salvage_worktree,
            salvage_host=salvage_host,
            replacement_limit=self.overlay.max_replacements,
        )
