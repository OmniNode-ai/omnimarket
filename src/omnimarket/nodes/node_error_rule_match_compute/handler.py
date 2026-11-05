# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure lookup over exact class keys, supplied versions and structured context."""

from omnimarket.models.error_rule_match import (
    ModelErrorRuleMatched,
    ModelErrorRuleMatchRequest,
    ModelErrorRuleMatchResult,
    ModelErrorRuleNoMatch,
)


class HandlerErrorRuleMatch:
    def handle(self, request: ModelErrorRuleMatchRequest) -> ModelErrorRuleMatchResult:
        normalized = request.normalized
        key = normalized.canonical_key
        context = request.context
        versions = {
            binding.row_version
            for binding in request.table.key_versions
            if binding.normalizer_version == normalized.key_version
        }
        matches = []
        for row in request.table.rows:
            if (
                key is None
                or key not in row.class_keys
                or row.key_version not in versions
            ):
                continue
            if row.trigger.kind != context.trigger_kind:
                continue
            conditions = row.applies_if
            if any(
                allowed and actual not in allowed
                for allowed, actual in (
                    (conditions.host_roles, context.host_role),
                    (conditions.repositories, context.repository),
                    (conditions.ticket_states, context.ticket_state),
                )
            ):
                continue
            if (
                row.trigger.state_check is not None
                and row.trigger.state_check not in context.state_checks
            ):
                continue
            matches.append(row)
        if len(matches) > 1:
            # Fail closed if the upstream validator was bypassed; table order
            # must never choose an arbitrary action.
            raise ValueError(
                "ambiguous error-rule lookup; supply a core-validated table"
            )
        if not matches:
            return ModelErrorRuleMatchResult(
                decision=ModelErrorRuleNoMatch(
                    class_key=key if key is not None else normalized.class_key,
                    key_version=normalized.key_version,
                )
            )
        row = matches[0]
        return ModelErrorRuleMatchResult(
            decision=ModelErrorRuleMatched(
                class_key=key,
                key_version=normalized.key_version,
                row_id=row.id,
                rules=row.sources,
                action=row.action,
                mode=row.mode,
            )
        )
