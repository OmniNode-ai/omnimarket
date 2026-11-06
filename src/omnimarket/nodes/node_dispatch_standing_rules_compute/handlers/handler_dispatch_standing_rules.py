# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evaluate the closed check table without reading a clock, file or environment."""

from omnimarket.models.standing_rules.model_standing_rules import (
    ModelStandingRule,
    ModelStandingRulesResult,
    ModelStandingRulesRoute,
    ModelStandingRulesSnapshot,
)


class HandlerDispatchStandingRules:
    """A lapsed rule cannot refuse; an applicable unread fact cannot allow."""

    def handle(self, request: ModelStandingRulesSnapshot) -> ModelStandingRulesResult:
        dispatch = request.dispatch
        lapsed: list[str] = []
        refused: ModelStandingRulesResult | None = None
        routed: ModelStandingRulesResult | None = None

        def answer(
            verdict: str, rule: ModelStandingRule, fix: str
        ) -> ModelStandingRulesResult:
            return ModelStandingRulesResult.model_validate(
                {
                    "request_id": dispatch.request_id,
                    "verdict": verdict,
                    "rule": rule.id,
                    "fix": fix,
                    "lapsed": tuple(lapsed),
                    "terminal_failure_cause": "unread" if verdict == "UNREAD" else None,
                }
            )

        def unread(rule: ModelStandingRule, name: str) -> ModelStandingRulesResult:
            binding = request.registry.facts.get(name)
            read = (
                binding.read
                if binding
                else f"read named fact {name} from standing_rules overlay"
            )
            return answer("UNREAD", rule, f"UNREAD {name}; FIX: {read}")

        for rule in request.registry.rules:
            if rule.source_ruling not in request.rulings:
                return answer(
                    "UNREAD",
                    rule,
                    f"UNREAD source ledger ruling; FIX: {request.registry.ledger.read}",
                )
            applies = (
                dispatch.dispatch_kind in rule.dispatch_kinds
                and dispatch.model in rule.models
                and dispatch.repo_only
                and not dispatch.lab_resources
            )
            expiry = rule.expires_from
            if expiry.kind == "time":
                expires_at = expiry.at
            else:
                fact = request.facts.get(expiry.fact)
                if fact is None or fact.expires_at is None:
                    if applies:
                        return unread(rule, expiry.fact)
                    continue
                expires_at = fact.expires_at
            if request.now >= expires_at:
                lapsed.append(rule.id)
                continue
            if not applies:
                continue
            credit = request.facts.get(rule.credit_fact)
            if credit is None or credit.remaining is None:
                return unread(rule, rule.credit_fact)
            if credit.remaining == 0:
                continue
            slot = request.facts.get(rule.slot_fact)
            if slot is None or slot.available is None:
                return unread(rule, rule.slot_fact)
            if not slot.available:
                continue
            if dispatch.placement == "lab":
                refused = answer("refuse", rule, rule.fix)
            elif dispatch.placement == "auto":
                if rule.mode == "refuse":
                    refused = answer("refuse", rule, rule.fix)
                else:
                    route = ModelStandingRulesRoute(row=dispatch, order=rule.order)
                    if routed is not None and routed.route != route:
                        return answer(
                            "UNREAD",
                            rule,
                            "UNREAD conflicting routes; FIX: read standing_rules overlay",
                        )
                    routed = ModelStandingRulesResult(
                        request_id=dispatch.request_id,
                        verdict="route",
                        rule=rule.id,
                        route=route,
                    )
        result = (
            refused
            or routed
            or ModelStandingRulesResult(request_id=dispatch.request_id, verdict="allow")
        )
        return result.model_copy(update={"lapsed": tuple(lapsed)})
