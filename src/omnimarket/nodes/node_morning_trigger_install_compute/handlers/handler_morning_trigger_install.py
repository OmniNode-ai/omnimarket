# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan what the morning-workflows installer copies for a mode (OMN-24424).

Moved from the frozen shell installer launchd/morning-workflows/morning_trigger_install.sh:
lib-only refreshes only the package lib and leaves the bin files (driver, deferral runner,
deferral template, watchdog driver) untouched and reloads no launchd label; install copies
the bin and lib files and reloads the labels. The mode rules arrive in the request, read
from the packaged overlay morning_install_policy.yaml (this handler reads no file).
"""

from __future__ import annotations

from omnimarket.models.model_morning_trigger_install import (
    ModelMorningInstallPlan,
    ModelMorningInstallRequest,
)


class HandlerMorningTriggerInstall:
    """Pure compute: a mode, its artifacts and its policy to the installer copy plan."""

    def handle(self, request: ModelMorningInstallRequest) -> ModelMorningInstallPlan:
        rule = request.policy.modes.get(request.mode)
        if rule is None:
            known = ", ".join(sorted(request.policy.modes))
            return ModelMorningInstallPlan(
                correlation_id=request.correlation_id,
                outcome="refused",
                mode=request.mode,
                refusal_code="unknown_mode",
                detail=(
                    f"mode {request.mode!r} is not a mode of the install policy; "
                    f"known modes: {known}"
                ),
            )
        return ModelMorningInstallPlan(
            correlation_id=request.correlation_id,
            outcome="planned",
            mode=request.mode,
            copy_actions=tuple(
                a for a in request.artifacts if a.kind in rule.artifact_kinds
            ),
            reload_labels=rule.reload_labels,
        )
