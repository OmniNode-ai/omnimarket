# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the declared bring-up order of a host returning from a long absence.

The order is the node overlay's ``roles`` mapping. Each step runs its unit's own installer
through the injected installer port, so a step has no second implementation here. A dry run
never invokes an installer in install mode: a step whose installer has no dry-run mode is
reported ``not_previewable`` and not run. A failed step stops the run and names the rollback.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.nodes.node_host_bringup_run_effect.models import (
    EnumBringupMode,
    EnumPreviewKind,
    EnumRollbackKind,
    EnumStepOutcome,
    ModelBringupStepDecl,
    ModelBringupStepResult,
    ModelHostBringupRunOverlay,
    ModelHostBringupRunRequest,
    ModelHostBringupRunResult,
)
from omnimarket.nodes.node_host_bringup_run_effect.protocols import (
    ProtocolDeployStoreReader,
    ProtocolEventPublisher,
    ProtocolHostInstaller,
)

_INSTALLER_MODE = {
    EnumBringupMode.DRY_RUN: "dry-run",
    EnumBringupMode.INSTALL: "install",
    EnumBringupMode.STATUS: "status",
    EnumBringupMode.ROLLBACK: "uninstall",
}
_CONTRACT = yaml.safe_load(
    (Path(__file__).parents[1] / "contract.yaml").read_text(encoding="utf-8")
)
COMPLETED_TOPIC = str(_CONTRACT["terminal_event"])
FAILED_TOPIC = str(_CONTRACT["failure_event"])
ENV_REFUSED_EXIT = 64
ROLE_REFUSED_EXIT = 1


class HandlerHostBringupRun:
    """handle(request) -> result; every effect goes through an injected port."""

    def __init__(
        self,
        *,
        overlay: ModelHostBringupRunOverlay,
        installer: ProtocolHostInstaller,
        deploy_store: ProtocolDeployStoreReader,
        publisher: ProtocolEventPublisher,
        environ: Mapping[str, str],
    ) -> None:
        self._overlay = overlay
        self._installer = installer
        self._deploy_store = deploy_store
        self._publisher = publisher
        self._environ = environ

    def handle(self, request: ModelHostBringupRunRequest) -> ModelHostBringupRunResult:
        """Run the sequence and publish its terminal event: completed, or failed."""
        result = self._run(request)
        self._publisher.publish(
            COMPLETED_TOPIC if result.completed else FAILED_TOPIC, result
        )
        return result

    def _run(self, request: ModelHostBringupRunRequest) -> ModelHostBringupRunResult:
        role = self._overlay.roles.get(request.role)
        if role is None:
            return self._result(
                request,
                [],
                ROLE_REFUSED_EXIT,
                refusal=f"the overlay declares no role {request.role}; it declares "
                f"{sorted(self._overlay.roles)}",
            )
        ordered = list(enumerate(role.steps, start=1))
        if request.mode is EnumBringupMode.ROLLBACK:
            ordered.reverse()
        results: list[ModelBringupStepResult] = []
        for number, step in ordered:
            result = self._step(request, number, step)
            results.append(result)
            if result.outcome in (EnumStepOutcome.FAILED, EnumStepOutcome.REFUSED_ENV):
                return self._result(
                    request,
                    results,
                    result.exit_code or 1,
                    stopped_at=number,
                    rollback_hint=(
                        None
                        if request.mode is EnumBringupMode.ROLLBACK
                        else f"run mode rollback for host {request.host_name} "
                        f"role {request.role}: it uninstalls the steps declared "
                        f"rollback=uninstall, last first"
                    ),
                )
        return self._result(request, results, 0)

    def _step(
        self,
        request: ModelHostBringupRunRequest,
        number: int,
        step: ModelBringupStepDecl,
    ) -> ModelBringupStepResult:
        mode = _INSTALLER_MODE[request.mode]

        def result(
            outcome: EnumStepOutcome,
            exit_code: int | None = None,
            detail: str = "",
            installer_mode: str | None = None,
        ) -> ModelBringupStepResult:
            return ModelBringupStepResult(
                number=number,
                unit=step.unit,
                installer_mode=installer_mode,
                outcome=outcome,
                exit_code=exit_code,
                detail=detail,
                proof=step.proof,
            )

        if request.mode is EnumBringupMode.ROLLBACK and step.rollback is not (
            EnumRollbackKind.UNINSTALL
        ):
            return result(
                EnumStepOutcome.NOT_ROLLED_BACK,
                detail="declared rollback=none: this step is not undone by the sequence",
            )
        if step.needs_deploy_store and not self._deploy_store.has_deploy_agent_store(
            request.host_name
        ):
            return result(
                EnumStepOutcome.SKIPPED_NO_DEPLOY_STORE,
                detail=f"no deploy agent env store declared for {request.host_name}",
            )
        missing = [name for name in step.env if not self._environ.get(name)]
        if missing:
            return result(
                EnumStepOutcome.REFUSED_ENV,
                ENV_REFUSED_EXIT,
                f"set {', '.join(missing)}",
            )
        if (
            request.mode is EnumBringupMode.DRY_RUN
            and step.preview is EnumPreviewKind.NONE
        ):
            return result(
                EnumStepOutcome.NOT_PREVIEWABLE,
                detail="the unit's installer has no dry-run mode; not run",
            )
        outcome = self._installer.run(
            step.unit, mode, request.host_name if step.host_name else None
        )
        # A passing installer's own line is its stdout; its stderr may carry only a tool warning.
        text = (
            outcome.stdout.strip() or outcome.stderr.strip()
            if outcome.exit_code == 0
            else outcome.stderr.strip() or outcome.stdout.strip()
        )
        lines = text.splitlines()
        return result(
            EnumStepOutcome.OK if outcome.exit_code == 0 else EnumStepOutcome.FAILED,
            outcome.exit_code,
            lines[-1][:200] if lines else "",
            installer_mode=mode,
        )

    @staticmethod
    def _result(
        request: ModelHostBringupRunRequest,
        steps: list[ModelBringupStepResult],
        exit_code: int,
        *,
        stopped_at: int | None = None,
        rollback_hint: str | None = None,
        refusal: str | None = None,
    ) -> ModelHostBringupRunResult:
        ran = any(step.installer_mode is not None for step in steps)
        return ModelHostBringupRunResult(
            host_name=request.host_name,
            role=request.role,
            mode=request.mode,
            steps=tuple(steps),
            exit_code=exit_code,
            completed=exit_code == 0,
            stopped_at=stopped_at,
            rollback_hint=rollback_hint,
            refusal=refusal,
            changed_host=ran
            and request.mode in (EnumBringupMode.INSTALL, EnumBringupMode.ROLLBACK),
        )
