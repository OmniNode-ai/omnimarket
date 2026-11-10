# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTopicProvisionPlan: decide which broker topics a runtime must provision.

Definition-B compute: ``handle(request: ModelTopicProvisionRequest) -> ModelTopicProvisionPlan``.
Pure and deterministic: no I/O, no clock. The same request gives byte-identical
plans in any input order.

Rules:
- ALL: every contract provisions (the legacy baseline).
- PROFILE: a contract provisions iff the active profile is in its runtime_profiles.
- PROFILE_AND_MANIFEST: profile match AND the contract name is in the lane manifest.

Skip reasons:
- OUTSIDE_PROFILE: the profile is not in the contract's runtime_profiles.
- NOT_IN_LANE_MANIFEST: the contract name is not in the lane manifest.

Refusals (TopicProvisionRuleError):
- PROFILE_AND_MANIFEST with an empty lane_manifest.
- lane_manifest names a contract not among request.contracts.
- mode is not ALL and the profile is not spelled in any contract's runtime_profiles.
"""

from __future__ import annotations

from omnimarket.nodes.node_topic_provision_plan_compute.models.enum_topic_provision import (
    EnumTopicProvisionMode,
    EnumTopicSkipReason,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_plan import (
    ModelTopicProvisionPlan,
    ModelTopicProvisionSkip,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_request import (
    ModelTopicProvisionRequest,
)


class TopicProvisionRuleError(ValueError):
    """Raised when the provision rule cannot be applied to the given request."""


def plan_topic_provision(
    request: ModelTopicProvisionRequest,
) -> ModelTopicProvisionPlan:
    """Compute which topics to provision under the given rule.

    Deterministic in any input order.
    """
    mode = request.rule.mode
    contracts = request.contracts
    profile = request.profile
    lane_manifest = request.lane_manifest
    live_topics = request.live_topics

    # Refusal checks.
    if mode == EnumTopicProvisionMode.PROFILE_AND_MANIFEST and not lane_manifest:
        raise TopicProvisionRuleError(
            "PROFILE_AND_MANIFEST mode requires a non-empty lane_manifest; "
            "a lane that names nothing must not silently provision nothing."
        )

    known_names = {c.contract_name for c in contracts}
    unknown = sorted(n for n in lane_manifest if n not in known_names)
    if unknown:
        raise TopicProvisionRuleError(
            f"lane_manifest names contracts not among request.contracts: {unknown}"
        )

    if mode != EnumTopicProvisionMode.ALL:
        any_profile_match = any(profile in c.runtime_profiles for c in contracts)
        if not any_profile_match:
            raise TopicProvisionRuleError(
                f"profile {profile!r} is not spelled in any contract's runtime_profiles; "
                "an unknown profile must refuse, not provision nothing."
            )

    # Collect all declared topics across ALL contracts (for declared_topic_count).
    all_declared_topics: set[str] = set()
    for contract in contracts:
        all_declared_topics.update(contract.topics)
    declared_topic_count = len(all_declared_topics)

    # Determine which contracts load under the rule.
    loaded_contracts: list[str] = []
    skipped: list[ModelTopicProvisionSkip] = []

    for contract in contracts:
        name = contract.contract_name
        if mode == EnumTopicProvisionMode.ALL:
            loaded_contracts.append(name)
        elif mode == EnumTopicProvisionMode.PROFILE:
            if profile in contract.runtime_profiles:
                loaded_contracts.append(name)
            else:
                skipped.append(
                    ModelTopicProvisionSkip(
                        contract_name=name,
                        reason=EnumTopicSkipReason.OUTSIDE_PROFILE,
                    )
                )
        else:  # PROFILE_AND_MANIFEST
            if profile not in contract.runtime_profiles:
                skipped.append(
                    ModelTopicProvisionSkip(
                        contract_name=name,
                        reason=EnumTopicSkipReason.OUTSIDE_PROFILE,
                    )
                )
            elif name not in lane_manifest:
                skipped.append(
                    ModelTopicProvisionSkip(
                        contract_name=name,
                        reason=EnumTopicSkipReason.NOT_IN_LANE_MANIFEST,
                    )
                )
            else:
                loaded_contracts.append(name)

    # Compute topics_to_provision: union of topics of loaded contracts, sorted unique.
    topics_to_provision: set[str] = set()
    for contract in contracts:
        if contract.contract_name in loaded_contracts:
            topics_to_provision.update(contract.topics)
    topics_sorted = tuple(sorted(topics_to_provision))

    # Live topic counts.
    live_topic_count = len(live_topics)
    live_set = set(live_topics)
    live_topics_under_rule = len(live_set & set(topics_sorted))
    live_topics_outside_rule = live_topic_count - live_topics_under_rule
    live_topics_undeclared = len(live_set - all_declared_topics)

    contracts_loaded = tuple(sorted(loaded_contracts))
    contracts_skipped = tuple(sorted(skipped, key=lambda s: s.contract_name))

    return ModelTopicProvisionPlan(
        profile=profile,
        mode=mode,
        topics_to_provision=topics_sorted,
        contracts_loaded=contracts_loaded,
        contracts_skipped=contracts_skipped,
        declared_topic_count=declared_topic_count,
        live_topic_count=live_topic_count,
        live_topics_under_rule=live_topics_under_rule,
        live_topics_outside_rule=live_topics_outside_rule,
        live_topics_undeclared=live_topics_undeclared,
    )


class HandlerTopicProvisionPlan:
    """Definition-B handler: ``handle(request) -> ModelTopicProvisionPlan``."""

    def handle(self, request: ModelTopicProvisionRequest) -> ModelTopicProvisionPlan:
        """Plan which topics to provision under the rule."""
        return plan_topic_provision(request)
