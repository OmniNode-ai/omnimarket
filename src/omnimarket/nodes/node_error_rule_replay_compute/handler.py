# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic corpus replay against a caller's frozen table and facts."""

from omnimarket.models.error_normalizer import ModelErrorClassNormalizeRequest
from omnimarket.models.error_rule_match import (
    ModelErrorRuleMatchRequest,
    ModelErrorRuleReplayRequest,
    ModelErrorRuleReplayResult,
)
from omnimarket.nodes.node_error_class_normalize_compute import (
    NodeErrorClassNormalizeCompute,
)
from omnimarket.nodes.node_error_rule_match_compute import NodeErrorRuleMatchCompute
from omnimarket.protocols.protocol_error_class_normalizer import (
    ProtocolErrorClassNormalizer,
)
from omnimarket.protocols.protocol_error_rule_match import ProtocolErrorRuleMatcher


class HandlerErrorRuleReplay:
    def __init__(
        self,
        *,
        normalizer: ProtocolErrorClassNormalizer | None = None,
        matcher: ProtocolErrorRuleMatcher | None = None,
    ) -> None:
        self._normalizer = (
            normalizer if normalizer is not None else NodeErrorClassNormalizeCompute()
        )
        self._matcher = matcher if matcher is not None else NodeErrorRuleMatchCompute()

    def handle(
        self, request: ModelErrorRuleReplayRequest
    ) -> ModelErrorRuleReplayResult:
        no_match_keys = set()
        hits = misses = false_matches = labelled = 0
        for record in sorted(request.records, key=lambda record: record.id):
            normalized = self._normalizer.handle(
                ModelErrorClassNormalizeRequest(
                    event=record.event,
                    facts=request.facts,
                    aliases=request.aliases,
                    key_version=request.key_version,
                )
            )
            result = self._matcher.handle(
                ModelErrorRuleMatchRequest(
                    normalized=normalized,
                    table=request.table,
                    context=record.context,
                )
            )
            decision = result.decision
            labelled += record.gold != "unlabelled"
            if decision.status == "MATCH":
                hits += 1
                false_matches += record.gold == "NO-MATCH" or (
                    record.gold == "MATCH" and record.expected_row_id != decision.row_id
                )
            else:
                misses += 1
                no_match_keys.add(decision.class_key)
        return ModelErrorRuleReplayResult(
            hits=hits,
            misses=misses,
            false_matches=false_matches,
            labelled=labelled,
            unlabelled=len(request.records) - labelled,
            no_match_keys=tuple(sorted(no_match_keys)),
        )
