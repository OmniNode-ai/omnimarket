# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a routing contract that sends a lab-passing task class to a cloud model first.

Operator ruling 2026-10-08T23:48:01Z (OMN-20422): lab models first for appropriate
tasks, with a scoring rubric. The contract declares the rubric: a policy naming
the lab tier and the adequacy bar, and per task class a verdict with the
measurement behind it. A class is released from lab-first only by a recorded
measurement below the bar; an unmeasured class keeps the default. For every
other routable class this check refuses:

- a ``tier_order`` whose first tier is not the lab tier (or that omits it);
- an ``entry_share`` that starts a fraction of requests on another tier;
- a model pin (per-class override, or the default reference when the class has
  none) that names a model the lab tier does not serve;
- a rubric whose declared verdict is not the one its measurement earns.

The ladder itself must open on the lab tier, because a class with no
``tier_order`` of its own runs in the ladder's declaration order. The handler is
pure: the two contracts arrive parsed, and nothing is read or written here.
"""

from __future__ import annotations

from pydantic import ValidationError

from omnimarket.models.delegation.model_lab_rubric import (
    EnumLabVerdict,
    ModelLabFirstPolicy,
    ModelLabRubric,
)
from omnimarket.nodes.node_lab_first_routing_check_compute.models.model_lab_first_routing_check import (
    EnumLabFirstFindingClass,
    ModelLabFirstFinding,
    ModelLabFirstRoutingRequest,
    ModelLabFirstRoutingResult,
)

POLICY_SUBJECT = "<policy>"


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


class HandlerLabFirstRoutingCheck:
    """Judge the routing contracts against the lab-first rule."""

    def handle(
        self, request: ModelLabFirstRoutingRequest
    ) -> ModelLabFirstRoutingResult:
        findings: list[ModelLabFirstFinding] = []
        contracts = request.task_class_contracts

        policy = self._policy(contracts, findings)
        ladder = self._ladder(request.routing_tiers)
        lab_models: frozenset[str] = frozenset()
        if policy is not None:
            lab_models = self._check_ladder(policy, ladder, findings)

        overrides = _mapping(contracts.get("task_model_overrides"))
        default_ref = contracts.get("default_task_model_ref")
        classes = _mapping(contracts.get("task_classes"))
        for name, raw_entry in classes.items():
            self._check_class(
                name,
                _mapping(raw_entry),
                policy,
                lab_models,
                overrides,
                default_ref,
                findings,
            )

        return ModelLabFirstRoutingResult(
            ok=not findings,
            checked_classes=len(classes),
            findings=tuple(findings),
        )

    @staticmethod
    def _policy(
        contracts: dict[str, object], findings: list[ModelLabFirstFinding]
    ) -> ModelLabFirstPolicy | None:
        try:
            return ModelLabFirstPolicy.model_validate(contracts.get("lab_first_policy"))
        except ValidationError as exc:
            findings.append(
                ModelLabFirstFinding(
                    task_class=POLICY_SUBJECT,
                    finding_class=EnumLabFirstFindingClass.POLICY_INVALID,
                    detail=f"lab_first_policy is missing or invalid: {exc.error_count()} error(s)",
                )
            )
            return None

    @staticmethod
    def _ladder(routing_tiers: dict[str, object]) -> list[tuple[str, frozenset[str]]]:
        ladder: list[tuple[str, frozenset[str]]] = []
        tiers = routing_tiers.get("tiers")
        for tier in tiers if isinstance(tiers, list) else ():
            tier_map = _mapping(tier)
            models = tier_map.get("models")
            ids = frozenset(
                model["id"]
                for model in (models if isinstance(models, list) else ())
                if isinstance(model, dict) and isinstance(model.get("id"), str)
            )
            if isinstance(tier_map.get("name"), str):
                ladder.append((str(tier_map["name"]), ids))
        return ladder

    @staticmethod
    def _check_ladder(
        policy: ModelLabFirstPolicy,
        ladder: list[tuple[str, frozenset[str]]],
        findings: list[ModelLabFirstFinding],
    ) -> frozenset[str]:
        by_name = dict(ladder)
        problem: str | None = None
        if policy.lab_tier not in by_name:
            problem = f"routing_tiers.yaml declares no tier {policy.lab_tier!r}"
        elif ladder[0][0] != policy.lab_tier:
            problem = (
                f"routing_tiers.yaml opens on {ladder[0][0]!r}, not the lab tier "
                f"{policy.lab_tier!r}; a class with no tier_order of its own runs "
                "in that order"
            )
        elif not by_name[policy.lab_tier]:
            problem = f"the lab tier {policy.lab_tier!r} serves no model"
        if problem is not None:
            findings.append(
                ModelLabFirstFinding(
                    task_class=POLICY_SUBJECT,
                    finding_class=EnumLabFirstFindingClass.POLICY_INVALID,
                    detail=problem,
                )
            )
        return by_name.get(policy.lab_tier, frozenset())

    def _check_class(
        self,
        name: str,
        entry: dict[str, object],
        policy: ModelLabFirstPolicy | None,
        lab_models: frozenset[str],
        overrides: dict[str, object],
        default_ref: object,
        findings: list[ModelLabFirstFinding],
    ) -> None:
        routable = entry.get("routing_availability") is None
        verdict = self._verdict(name, entry, routable, policy, findings)
        if not routable or verdict is EnumLabVerdict.FAIL or policy is None:
            return

        policy_block = _mapping(entry.get("escalation_policy"))
        order = policy_block.get("tier_order")
        if isinstance(order, list) and (not order or order[0] != policy.lab_tier):
            findings.append(
                ModelLabFirstFinding(
                    task_class=name,
                    finding_class=EnumLabFirstFindingClass.CLOUD_FIRST_TIER_ORDER,
                    detail=(
                        f"tier_order {order} does not open on the lab tier "
                        f"{policy.lab_tier!r} and the class has no measured fail"
                    ),
                )
            )

        share = _mapping(policy_block.get("entry_share"))
        share_fraction = share.get("share")
        if (
            share
            and isinstance(share_fraction, int | float)
            and share_fraction > 0
            and share.get("tier") != policy.lab_tier
        ):
            findings.append(
                ModelLabFirstFinding(
                    task_class=name,
                    finding_class=EnumLabFirstFindingClass.ENTRY_SHARE_OFF_LAB,
                    detail=(
                        f"entry_share starts {share_fraction} of requests on "
                        f"{share.get('tier')!r}, not the lab tier"
                    ),
                )
            )

        pin = overrides.get(name, default_ref)
        if isinstance(pin, str) and lab_models and pin not in lab_models:
            findings.append(
                ModelLabFirstFinding(
                    task_class=name,
                    finding_class=EnumLabFirstFindingClass.NON_LAB_MODEL_PIN,
                    detail=f"model pin {pin!r} is not served by the lab tier",
                )
            )

    @staticmethod
    def _verdict(
        name: str,
        entry: dict[str, object],
        routable: bool,
        policy: ModelLabFirstPolicy | None,
        findings: list[ModelLabFirstFinding],
    ) -> EnumLabVerdict:
        """The verdict a class's rule runs on: the one its measurement earns."""
        raw = entry.get("lab_rubric")
        if raw is None:
            if routable:
                findings.append(
                    ModelLabFirstFinding(
                        task_class=name,
                        finding_class=EnumLabFirstFindingClass.RUBRIC_MISSING,
                        detail="a routable class declares no lab_rubric",
                    )
                )
            return EnumLabVerdict.UNMEASURED
        try:
            rubric = ModelLabRubric.model_validate(raw)
        except ValidationError as exc:
            findings.append(
                ModelLabFirstFinding(
                    task_class=name,
                    finding_class=EnumLabFirstFindingClass.RUBRIC_INCONSISTENT,
                    detail=f"lab_rubric does not load: {exc.error_count()} error(s)",
                )
            )
            return EnumLabVerdict.UNMEASURED
        if policy is None:
            return rubric.verdict
        earned = rubric.derived_verdict(policy.bar)
        if earned is not rubric.verdict:
            findings.append(
                ModelLabFirstFinding(
                    task_class=name,
                    finding_class=EnumLabFirstFindingClass.RUBRIC_INCONSISTENT,
                    detail=(
                        f"declared verdict {rubric.verdict.value!r} but the "
                        f"measurement earns {earned.value!r} against the bar "
                        f"{policy.bar.adequate} of {policy.bar.of}"
                    ),
                )
            )
        return earned
