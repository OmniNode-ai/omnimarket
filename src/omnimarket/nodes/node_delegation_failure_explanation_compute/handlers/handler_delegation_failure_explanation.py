# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explain recorded delegation failures as commentary, grouped by recorded cause (OMN-18538)."""

from collections import defaultdict
from pathlib import Path
from typing import Protocol

import yaml

from omnimarket.nodes.node_delegation_failure_explanation_compute.models.model_delegation_failure_explanation import (
    ModelDelegationFailureExplanation,
    ModelDelegationFailureExplanationRequest,
    ModelExplanationRules,
    ModelFailureAccountGroup,
    ModelFailureGroupFacts,
    ModelRecordedDelegationFailure,
)

_CONTRACT = Path(__file__).parents[1] / "contract.yaml"


class ProtocolFailureNarrator(Protocol):
    """Optional wording of a group with a recorded cause; its text never feeds the grouping."""

    def narrate(self, facts: ModelFailureGroupFacts) -> str:
        """Return prose for one group from its recorded facts only."""
        ...


def load_rules(contract: Path = _CONTRACT) -> ModelExplanationRules:
    """Read the explanation_rules of a contract; an unknown or missing rule is refused."""
    data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(
        data.get("explanation_rules"), dict
    ):
        raise ValueError("contract explanation_rules must be a mapping")
    return ModelExplanationRules.model_validate(data["explanation_rules"])


def _distinct(values: list[str]) -> tuple[str, ...]:
    return tuple(sorted({v for v in values if v}))


class HandlerDelegationFailureExplanation:
    """Pure compute over recorded failures and the contract's explanation_rules."""

    def __init__(
        self,
        rules: ModelExplanationRules | None = None,
        narrator: ProtocolFailureNarrator | None = None,
    ) -> None:
        self._rules = rules if rules is not None else load_rules()
        self._narrator = narrator

    def handle(
        self, request: ModelDelegationFailureExplanationRequest
    ) -> ModelDelegationFailureExplanation:
        """Group by recorded cause, state each group, and say unknown where no cause was recorded."""
        buckets: dict[str, list[ModelRecordedDelegationFailure]] = defaultdict(list)
        for failure in request.failures:
            buckets[self._group_key(failure)].append(failure)
        unknown_key = self._rules.unknown_group_key
        # Recorded causes sorted, the unknown group last: order never depends on generated text.
        keys = sorted(k for k in buckets if k != unknown_key)
        if unknown_key in buckets:
            keys.append(unknown_key)
        groups = tuple(self._group(k, buckets[k]) for k in keys)
        unknown_count = len(buckets.get(unknown_key, ()))
        summary = self._rules.templates.summary.format(
            total=len(request.failures), groups=len(groups), unknown=unknown_count
        )
        return ModelDelegationFailureExplanation(
            correlation_id=request.correlation_id,
            groups=groups,
            total_failures=len(request.failures),
            summary=summary,
        )

    def _group_key(self, failure: ModelRecordedDelegationFailure) -> str:
        """The first non-blank recorded field of group_key_fields; none is the unknown group."""
        for field in self._rules.group_key_fields:
            value = str(getattr(failure, field)).strip()
            if value:
                return value
        return self._rules.unknown_group_key

    def _group(
        self, key: str, failures: list[ModelRecordedDelegationFailure]
    ) -> ModelFailureAccountGroup:
        templates = self._rules.templates
        cause_recorded = key != self._rules.unknown_group_key
        ids = tuple(f.correlation_id for f in failures)
        facts = ModelFailureGroupFacts(
            group_key=key,
            cause_recorded=cause_recorded,
            count=len(failures),
            correlation_ids=ids,
            statuses=_distinct([f.terminal_status for f in failures]),
            backends=_distinct([f.backend_id for f in failures]),
            steps=_distinct([f.step_kind for f in failures]),
        )
        # A narrator is never asked about a failure with no recorded cause: nothing there to narrate.
        if cause_recorded and self._narrator is not None:
            account = self._narrator.narrate(facts)
        else:
            account = (templates.known if cause_recorded else templates.unknown).format(
                count=facts.count,
                cause=key,
                statuses=", ".join(facts.statuses) or "-",
                backends=", ".join(facts.backends) or "-",
                steps=", ".join(facts.steps) or "-",
                ids=", ".join(ids[: self._rules.max_listed_correlation_ids])
                or templates.none_listed,
            )
        return ModelFailureAccountGroup(
            group_key=key,
            cause_recorded=cause_recorded,
            count=facts.count,
            correlation_ids=ids,
            account=account,
        )
