# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Name the kind of a failed check from its name by an overlay rule table, before any model call.

The rule table (each rule's kind, its pattern over check names, its pattern
over the failed job's log excerpt, and its case-sensitivity) comes from the
overlay; nothing here names a pattern, a kind or a check. An overlay that is missing, unreadable or invalid is a
typed refusal; an absent overlay is ``OVERLAY_UNBOUND`` and a broken one is
``OVERLAY_INVALID``. Rules are tried in declared order and the first whose
every pattern is found in its own field (the check name, the log excerpt) decides
the kind; a name no
rule finds is answered as needing the model.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.enum_ci_red_kind_rule import (
    EnumCiRedKindRuleRefusal,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_answer import (
    ModelCiRedKindRuleAnswer,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_overlay import (
    ModelCiRedKindRule,
    ModelCiRedKindRuleOverlay,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_request import (
    ModelCiRedKindRuleRequest,
)

OVERLAY_NODE_NAME = "node_ci_red_kind_rule_compute"
OVERLAY_FILE_ENV = "CI_RED_KIND_RULES_OVERLAY"


def load_ci_red_kind_rule_overlay() -> ModelCiRedKindRuleOverlay:
    """The node's rule table; refuses when no overlay supplies one."""
    return load_structured_overlay(
        OVERLAY_NODE_NAME,
        ModelCiRedKindRuleOverlay,
        file_env=OVERLAY_FILE_ENV,
    )


class HandlerCiRedKindRule:
    """Name the kind of a failed check from its name by an overlay rule table."""

    def __init__(
        self,
        load_overlay: Callable[
            [], ModelCiRedKindRuleOverlay
        ] = load_ci_red_kind_rule_overlay,
    ) -> None:
        self._load_overlay = load_overlay

    def handle(self, request: ModelCiRedKindRuleRequest) -> ModelCiRedKindRuleAnswer:
        try:
            overlay = self._load_overlay()
        except NodeOverlayNotFoundError as exc:
            return self._refusal(EnumCiRedKindRuleRefusal.OVERLAY_UNBOUND, str(exc))
        except NodeOverlayError as exc:
            return self._refusal(EnumCiRedKindRuleRefusal.OVERLAY_INVALID, str(exc))
        for index, rule in enumerate(overlay.rules):
            if self._rule_matches(rule, request):
                return ModelCiRedKindRuleAnswer(
                    ok=True,
                    matched=True,
                    kind=rule.kind,
                    rule_index=index,
                    model_call_required=False,
                    refusal=None,
                    detail="",
                )
        return ModelCiRedKindRuleAnswer(
            ok=True,
            matched=False,
            kind=None,
            rule_index=None,
            model_call_required=True,
            refusal=None,
            detail="",
        )

    @staticmethod
    def _rule_matches(
        rule: ModelCiRedKindRule,
        request: ModelCiRedKindRuleRequest,
    ) -> bool:
        flags = re.IGNORECASE if rule.ignore_case else 0
        fields = (
            (rule.check_name_pattern, request.check_name),
            (rule.log_pattern, request.log_excerpt),
        )
        return all(
            re.compile(pattern, flags).search(text)
            for pattern, text in fields
            if pattern is not None
        )

    def _refusal(
        self,
        refusal: EnumCiRedKindRuleRefusal,
        detail: str,
    ) -> ModelCiRedKindRuleAnswer:
        return ModelCiRedKindRuleAnswer(
            ok=False,
            matched=False,
            kind=None,
            rule_index=None,
            model_call_required=False,
            refusal=refusal,
            detail=detail,
        )


__all__: list[str] = [
    "OVERLAY_FILE_ENV",
    "OVERLAY_NODE_NAME",
    "HandlerCiRedKindRule",
    "load_ci_red_kind_rule_overlay",
]
