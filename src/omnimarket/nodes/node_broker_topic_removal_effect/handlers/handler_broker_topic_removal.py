# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan, and with the operator's approval carry out, the removal of never-produced broker topics (OMN-23697).

Every topic on the broker is classified four ways: declared or undeclared by a contract, and produced or
never produced by the broker's own high watermarks. The listing decision is the never-produced listing's
(OMN-23614), run with the overlay's rule; the removal only adds the contracts' declarations on top. A dry run
plans and touches nothing. An execute run removes the candidates only when the overlay enables it, the
command names an approval, a remover is wired and the plan fits the per-run cap; every other way it ends is
a typed refusal with the plan it had reached.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from omnimarket.events.topic_never_produced import (
    ModelNeverProducedRequest,
    ModelTopicEvidence,
)
from omnimarket.events.topic_removal import (
    EnumRemovalMode,
    EnumTopicClass,
    ModelClassCount,
    ModelRefusalCount,
    ModelRemovalCandidate,
    ModelTopicObservation,
    ModelTopicRemovalCommand,
    ModelTopicRemovalResult,
)
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    ModelTopicRemovalOverlay,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    DeclaredTopicsError,
    ProtocolDeclaredTopics,
    ProtocolTopicInventory,
    ProtocolTopicRemover,
    TopicInventoryError,
    TopicRemovalError,
)
from omnimarket.nodes.node_topic_never_produced_compute.handlers.handler_topic_never_produced import (
    HandlerTopicNeverProduced,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerBrokerTopicRemoval:
    """Plan once, or carry out the planned removal."""

    def __init__(
        self,
        *,
        overlay: ModelTopicRemovalOverlay,
        inventory: ProtocolTopicInventory,
        declared: ProtocolDeclaredTopics,
        remover: ProtocolTopicRemover | None,
    ) -> None:
        self._overlay = overlay
        self._inventory = inventory
        self._declared = declared
        self._remover = remover

    def _refused(
        self,
        command: ModelTopicRemovalCommand,
        reasons: tuple[str, ...],
    ) -> ModelTopicRemovalResult:
        return ModelTopicRemovalResult(
            correlation_id=command.correlation_id,
            mode=command.mode,
            status="refused",
            refusal_reasons=reasons,
        )

    def _result(
        self,
        command: ModelTopicRemovalCommand,
        status: Literal["planned", "removed", "refused"],
        refusal_reasons: tuple[str, ...],
        plan: ModelTopicRemovalResult,
        removed: tuple[str, ...],
        deletes_issued: int,
    ) -> ModelTopicRemovalResult:
        """The plan reached, carried to ``status`` with its refusal, removals and delete count."""
        return plan.model_copy(
            update={
                "status": status,
                "refusal_reasons": refusal_reasons,
                "removed": removed,
                "deletes_issued": deletes_issued,
            }
        )

    def handle(self, command: ModelTopicRemovalCommand) -> ModelTopicRemovalResult:
        if command.mode is EnumRemovalMode.EXECUTE:
            reasons: list[str] = []
            if not self._overlay.execute_enabled:
                reasons.append("execute_disabled_by_overlay")
            if command.approval_ref is None or not command.approval_ref.strip():
                reasons.append("approval_missing")
            if self._remover is None:
                reasons.append("remover_not_wired")
            if reasons:
                return self._refused(command, tuple(reasons))

        try:
            observations = list(self._inventory.read())
        except TopicInventoryError as exc:
            return self._refused(command, (f"inventory_unreadable:{exc}",))
        try:
            declared = dict(self._declared.declared())
            producers = dict(self._declared.producers())
        except DeclaredTopicsError as exc:
            return self._refused(command, (f"declared_topics_unreadable:{exc}",))

        declared_names = set(declared)
        by_name = {obs.name: obs for obs in observations}
        evidence = tuple(
            ModelTopicEvidence(
                name=obs.name,
                partitions=obs.partitions,
                high_watermark_total=obs.high_watermark_total,
                records_in_retention=0
                if obs.high_watermark_total == 0
                else obs.high_watermark_total,
                consumer_groups=obs.consumer_groups,
                declared_producers=producers.get(obs.name, ()),
            )
            for obs in observations
        )
        listing = HandlerTopicNeverProduced().handle(
            ModelNeverProducedRequest(rule=self._overlay.listing_rule, topics=evidence)
        )
        listed_names = {entry.name for entry in listing.listed}

        def topic_class(obs: ModelTopicObservation) -> EnumTopicClass:
            never_produced = obs.high_watermark_total == 0
            is_declared = obs.name in declared_names
            if is_declared:
                return (
                    EnumTopicClass.DECLARED_NEVER_PRODUCED
                    if never_produced
                    else EnumTopicClass.DECLARED_PRODUCED
                )
            return (
                EnumTopicClass.UNDECLARED_NEVER_PRODUCED
                if never_produced
                else EnumTopicClass.UNDECLARED_PRODUCED
            )

        classes = tuple(
            ModelClassCount(
                topic_class=cls,
                topics=sum(1 for obs in observations if topic_class(obs) is cls),
                partitions=sum(
                    obs.partitions for obs in observations if topic_class(obs) is cls
                ),
                produced_bytes=sum(
                    obs.produced_bytes or 0
                    for obs in observations
                    if topic_class(obs) is cls
                ),
            )
            for cls in EnumTopicClass
        )

        candidates: list[ModelRemovalCandidate] = []
        for entry in listing.listed:
            if (
                entry.name in declared_names
                and not self._overlay.remove_declared_topics
            ):
                continue
            obs = by_name[entry.name]
            if entry.name in declared_names:
                contract_names = ", ".join(sorted(declared[entry.name]))
                evidence_ = (*entry.evidence, f"declared by: {contract_names}")
            else:
                evidence_ = (*entry.evidence, "no contract declares it")
            candidates.append(
                ModelRemovalCandidate(
                    name=entry.name,
                    partitions=obs.partitions,
                    topic_class=topic_class(obs),
                    evidence=evidence_,
                )
            )
        candidate_names = {candidate.name for candidate in candidates}

        refusal: dict[str, list[ModelTopicObservation]] = {}
        for skipped in listing.not_listed:
            if skipped.reason == "produced":
                continue  # a produced topic is outside the plan, not a refusal
            obs = by_name[skipped.name]
            if obs.high_watermark_total:
                continue  # a topic with a watermark is produced, and so is outside the plan
            refusal.setdefault(skipped.reason, []).append(obs)
        for obs in observations:
            if (
                obs.name in listed_names
                and obs.name in declared_names
                and obs.name not in candidate_names
            ):
                refusal.setdefault("contract_declared", []).append(obs)
        refused = tuple(
            ModelRefusalCount(
                reason=reason,
                topics=len(observed),
                partitions=sum(obs.partitions for obs in observed),
            )
            for reason, observed in sorted(refusal.items())
        )

        candidate_partitions = sum(candidate.partitions for candidate in candidates)
        partitions_before = sum(obs.partitions for obs in observations)
        plan = ModelTopicRemovalResult(
            correlation_id=command.correlation_id,
            mode=command.mode,
            status="planned",
            topics_before=len(observations),
            partitions_before=partitions_before,
            classes=classes,
            candidates=tuple(candidates),
            refused=refused,
            candidate_partitions=candidate_partitions,
            partitions_after=partitions_before - candidate_partitions,
            max_topics_per_run=self._overlay.max_topics_per_run,
            over_run_cap=len(candidates) > self._overlay.max_topics_per_run,
        )
        if command.mode is EnumRemovalMode.DRY_RUN:
            return self._result(command, "planned", (), plan, (), 0)
        if plan.over_run_cap:
            return self._result(command, "refused", ("over_run_cap",), plan, (), 0)
        assert self._remover is not None
        removed: tuple[str, ...] = ()
        if candidates:
            try:
                removed = tuple(
                    sorted(self._remover.remove([c.name for c in candidates]))
                )
            except TopicRemovalError as exc:
                return self._result(
                    command, "refused", (f"remove_failed:{exc}",), plan, (), 0
                )
        return self._result(command, "removed", (), plan, removed, len(candidates))


def events_of(
    result: ModelTopicRemovalResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "mode": result.mode.value,
        "status": result.status,
        "refusal_reasons": list(result.refusal_reasons),
        "topics_before": result.topics_before,
        "partitions_before": result.partitions_before,
        "classes": [
            {
                "topic_class": entry.topic_class.value,
                "topics": entry.topics,
                "partitions": entry.partitions,
                "produced_bytes": entry.produced_bytes,
            }
            for entry in result.classes
        ],
        "candidates": [
            {
                "name": candidate.name,
                "partitions": candidate.partitions,
                "topic_class": candidate.topic_class.value,
                "evidence": list(candidate.evidence),
            }
            for candidate in result.candidates
        ],
        "refused": [
            {
                "reason": entry.reason,
                "topics": entry.topics,
                "partitions": entry.partitions,
            }
            for entry in result.refused
        ],
        "candidate_partitions": result.candidate_partitions,
        "partitions_after": result.partitions_after,
        "max_topics_per_run": result.max_topics_per_run,
        "over_run_cap": result.over_run_cap,
        "removed": list(result.removed),
        "deletes_issued": result.deletes_issued,
    }
    role = (
        "broker_topic_removal_refused"
        if result.status == "refused"
        else "broker_topic_removal_planned"
    )
    return [
        (topics[role], body),
        (
            topics["broker_topic_removal_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerBrokerTopicRemoval, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelTopicRemovalCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [
            (topics["broker_topic_removal_run_failed"], json.dumps(failure).encode())
        ]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = ["HandlerBrokerTopicRemoval", "answer_command", "events_of", "load_topics"]
