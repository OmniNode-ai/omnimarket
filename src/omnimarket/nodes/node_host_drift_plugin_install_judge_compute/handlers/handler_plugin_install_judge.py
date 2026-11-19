# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge one plugin repair attempt from the installed record before and after it (OMN-24194).

On h101 on 2026-11-13 the plugin tree install ran three times on one commit, each reported as applied, while the installed
record stayed at version 0.4.475 and the item stayed open for four hours. An attempt that leaves the installed revision and
version unchanged repaired nothing: it is alert only with that reason, spends no fix attempt and is not retried identically.
"""

from __future__ import annotations

from omnimarket.models.model_plugin_install_judgement import (
    EnumPluginInstallJudgement,
    ModelPluginInstallJudgement,
    ModelPluginInstallObservation,
    ModelPluginInstallSnapshot,
)

UNREAD = "unread"


def _held(snapshot: ModelPluginInstallSnapshot) -> str:
    revision = snapshot.revision[:12] if snapshot.revision else UNREAD
    return f"revision {revision} version {snapshot.version or UNREAD}"


class HandlerHostDriftPluginInstallJudge:
    """Pure compute: the installer's exit code and the record before and after to one judgement."""

    def handle(
        self, request: ModelPluginInstallObservation
    ) -> ModelPluginInstallJudgement:
        def judged(
            outcome: EnumPluginInstallJudgement,
            counts: bool,
            retry: bool,
            detail: str,
        ) -> ModelPluginInstallJudgement:
            return ModelPluginInstallJudgement(
                name=request.name,
                outcome=outcome,
                counts_as_attempt=counts,
                retry_allowed=retry,
                detail=detail,
            )

        if request.exit_code != 0:
            return judged(
                EnumPluginInstallJudgement.FAILED,
                True,
                True,
                f"{request.action} exited {request.exit_code}",
            )
        if request.after is None:
            return judged(
                EnumPluginInstallJudgement.ALERT_ONLY,
                False,
                False,
                f"{request.action} exited 0 but the installed record could not be read after it; "
                f"it held {_held(request.before)} before",
            )
        if request.after == request.before:
            return judged(
                EnumPluginInstallJudgement.ALERT_ONLY,
                False,
                False,
                f"{request.action} left the installed record unchanged at "
                f"{_held(request.before)}: the same install again would change nothing",
            )
        return judged(
            EnumPluginInstallJudgement.APPLIED,
            True,
            True,
            f"{request.action} moved the installed record from {_held(request.before)} "
            f"to {_held(request.after)}",
        )
