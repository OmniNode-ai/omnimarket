# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S9 never treats caller-supplied actor text as writer identity."""

from omnimarket.models.host_checks import (
    ModelHostCheckFinding,
    ModelInstallClaimRequest,
)


class HandlerInstallClaimCheck:
    def handle(self, request: ModelInstallClaimRequest) -> ModelHostCheckFinding:
        claim, host = request.claim, request.host
        contained = (
            host.contains.get(claim.sha)
            if host is not None and host.host == claim.host and host.installed_revision
            else None
        )
        if not request.install_principals:
            verdict, reason = "UNREAD", "install-principals-unread"
        elif (
            claim.writer_principal not in request.install_principals
            or contained is False
        ):
            verdict, reason = "FINDING", "claimed-installed-but-absent"
        elif contained is None:
            verdict, reason = "UNREAD", "installed-revision-unread"
        else:
            verdict, reason = "OK", "workflow-install-read-back"
        return ModelHostCheckFinding.model_validate(
            {
                "check": "S9",
                "host": claim.host,
                "verdict": verdict,
                "reason": reason,
                "key": f"S9:{claim.row_id}:{claim.host}:{claim.sha}",
                "claim_id": claim.row_id,
                "fix": request.fix if verdict != "OK" else "",
                "accepted": verdict == "OK",
            }
        )
