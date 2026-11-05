# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S7 compares a read login label with the deployment's pool labels."""

from omnimarket.models.host_checks import ModelHostAccountRequest, ModelHostCheckFinding


class HandlerHostAccountCheck:
    def handle(self, request: ModelHostAccountRequest) -> ModelHostCheckFinding:
        label = request.host.account_label
        if not label or not request.pool_accounts:
            verdict, reason = "UNREAD", "host-account-unread"
        elif label in request.pool_accounts:
            verdict, reason = "OK", "host-account-in-pool"
        else:
            verdict, reason = "FINDING", "host-account-outside-pool"
        return ModelHostCheckFinding.model_validate(
            {
                "check": "S7",
                "host": request.host.host,
                "verdict": verdict,
                "reason": reason,
                "key": f"S7:{request.host.host}:{label}",
                "account_label": label,
                "fix": request.token_switch_fix if verdict != "OK" else "",
            }
        )
