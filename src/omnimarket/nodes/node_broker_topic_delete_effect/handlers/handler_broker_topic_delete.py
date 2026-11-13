# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delete exactly the topics a plan named, and only those, when the overlay enforces (OMN-23879).

The command names the topics and the class the plan gave each; this handler never plans. It reads the
broker's live evidence and the contracts' declarations fresh, refuses each topic for typed reasons —
a protected prefix, a produced class, a contract that contradicts the command, live consumers or
committed offsets, retained records or a producer still inside the recency window — and deletes only
what nothing refuses. A report-only run names what would go and never calls the broker's delete.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    EnumTopicClass,
    EnumTopicDeleteStatus,
    ModelDeleteTarget,
    ModelLiveTopicEvidence,
    ModelTopicDeleteCommand,
    ModelTopicDeleteEntry,
    ModelTopicDeleteResult,
)
from omnimarket.nodes.node_broker_topic_delete_effect.overlay import (
    ModelTopicDeleteOverlay,
)
from omnimarket.nodes.node_broker_topic_delete_effect.protocols import (
    BrokerAdminError,
    DeclaredTopicsError,
    ProtocolBrokerTopicAdmin,
    ProtocolDeclaredTopics,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerBrokerTopicDelete:
    """Refuse every named topic the live state still holds against, delete the rest."""

    def __init__(
        self,
        *,
        overlay: ModelTopicDeleteOverlay,
        admin: ProtocolBrokerTopicAdmin,
        declared: ProtocolDeclaredTopics,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._admin = admin
        self._declared = declared
        self._clock = clock

    def _refused(
        self, command: ModelTopicDeleteCommand, reasons: tuple[str, ...]
    ) -> ModelTopicDeleteResult:
        return ModelTopicDeleteResult(
            correlation_id=command.correlation_id,
            status="refused",
            refusal_reasons=reasons,
        )

    def _reasons(
        self,
        command: ModelTopicDeleteCommand,
        target: ModelDeleteTarget,
        evidence: ModelLiveTopicEvidence,
        declared_names: frozenset[str],
    ) -> tuple[str, ...]:
        """Every typed reason the live state holds against deleting one named topic."""
        reasons: list[str] = []
        if any(
            target.name.startswith(prefix)
            for prefix in self._overlay.protected_prefixes
        ):
            reasons.append("protected_prefix")
        if target.topic_class in (
            EnumTopicClass.DECLARED_PRODUCED,
            EnumTopicClass.UNDECLARED_PRODUCED,
        ):
            reasons.append("class_not_removable")
        if target.name in declared_names:
            if target.topic_class is EnumTopicClass.UNDECLARED_NEVER_PRODUCED:
                reasons.append("declared_contradicts_command")
            elif target.topic_class is EnumTopicClass.DECLARED_NEVER_PRODUCED and not (
                command.ruling_covers_declared
                and command.ruling_ref is not None
                and command.ruling_ref.strip()
            ):
                reasons.append("declared_not_cleared")
        reasons.extend(f"consumer_group:{group}" for group in evidence.consumer_groups)
        reasons.extend(f"committed_offsets:{group}" for group in evidence.offset_groups)
        if evidence.high_watermark_total > 0:
            if evidence.retained_records > 0:
                reasons.append("holds_records")
            elif evidence.last_record_ms is None:
                reasons.append("produced_before_unknown_age")
            if evidence.last_record_ms is not None:
                recorded = datetime.fromtimestamp(
                    evidence.last_record_ms / 1000, tz=UTC
                )
                if self._clock() - recorded < timedelta(
                    days=self._overlay.recent_producer_days
                ):
                    reasons.append("producer_within_window")
        return tuple(sorted(set(reasons)))

    def handle(self, command: ModelTopicDeleteCommand) -> ModelTopicDeleteResult:
        if self._overlay.enforce and (
            command.approval_ref is None or not command.approval_ref.strip()
        ):
            return self._refused(command, ("approval_missing",))
        if len(command.topics) > self._overlay.max_topics_per_command:
            return self._refused(command, ("over_command_cap",))
        try:
            declared = dict(self._declared.declared())
        except DeclaredTopicsError as exc:
            return self._refused(command, (f"declared_topics_unreadable:{exc}",))
        names = [target.name for target in command.topics]
        try:
            evidence = self._admin.read_evidence(names)
        except BrokerAdminError as exc:
            return self._refused(command, (f"broker_unreadable:{exc}",))
        missing = sorted(set(names) - set(evidence))
        if missing:
            return self._refused(
                command, (f"broker_unreadable:evidence_missing:{','.join(missing)}",)
            )
        declared_names = frozenset(declared)

        entries: list[ModelTopicDeleteEntry] = []
        eligible: list[ModelDeleteTarget] = []
        for target in sorted(command.topics, key=lambda target: target.name):
            live = evidence[target.name]
            if not live.exists:
                entries.append(
                    ModelTopicDeleteEntry(
                        name=target.name,
                        status=EnumTopicDeleteStatus.ABSENT,
                        partitions=live.partitions,
                    )
                )
                continue
            reasons = self._reasons(command, target, live, declared_names)
            if reasons:
                entries.append(
                    ModelTopicDeleteEntry(
                        name=target.name,
                        status=EnumTopicDeleteStatus.REFUSED,
                        reasons=reasons,
                        partitions=live.partitions,
                    )
                )
                continue
            eligible.append(target)

        if not self._overlay.enforce:
            entries.extend(
                ModelTopicDeleteEntry(
                    name=target.name,
                    status=EnumTopicDeleteStatus.WOULD_DELETE,
                    partitions=evidence[target.name].partitions,
                )
                for target in eligible
            )
            entries.sort(key=lambda entry: entry.name)
            return ModelTopicDeleteResult(
                correlation_id=command.correlation_id,
                status="report_only",
                entries=tuple(entries),
            )

        outcomes: Mapping[str, str] = {}
        confirmations: Mapping[str, ModelLiveTopicEvidence] = {}
        if eligible:
            try:
                outcomes = self._admin.delete([target.name for target in eligible])
                confirmations = self._admin.read_evidence(
                    [target.name for target in eligible]
                )
            except BrokerAdminError as exc:
                return self._refused(command, (f"delete_failed:{exc}",))
        entries.extend(
            ModelTopicDeleteEntry(
                name=target.name,
                status=self._deleted_status(target.name, outcomes, confirmations),
                reasons=self._delete_reasons(target.name, outcomes, confirmations),
                partitions=evidence[target.name].partitions,
            )
            for target in eligible
        )
        entries.sort(key=lambda entry: entry.name)
        return ModelTopicDeleteResult(
            correlation_id=command.correlation_id,
            status="enforced",
            entries=tuple(entries),
            deletes_issued=len(eligible),
            partitions_removed=sum(
                entry.partitions
                for entry in entries
                if entry.status is EnumTopicDeleteStatus.DELETED
            ),
        )

    @staticmethod
    def _delete_reasons(
        name: str,
        outcomes: Mapping[str, str],
        confirmations: Mapping[str, ModelLiveTopicEvidence],
    ) -> tuple[str, ...]:
        """Why the broker could not delete one topic it was asked to; empty when it did."""
        outcome = outcomes.get(name, "delete_unconfirmed")
        if outcome:
            return (f"delete_failed:{outcome}",)
        confirmed = confirmations.get(name)
        if confirmed is None or confirmed.exists:
            return ("still_present",)
        return ()

    @classmethod
    def _deleted_status(
        cls,
        name: str,
        outcomes: Mapping[str, str],
        confirmations: Mapping[str, ModelLiveTopicEvidence],
    ) -> EnumTopicDeleteStatus:
        return (
            EnumTopicDeleteStatus.DELETED
            if not cls._delete_reasons(name, outcomes, confirmations)
            else EnumTopicDeleteStatus.FAILED
        )


def events_of(
    result: ModelTopicDeleteResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "status": result.status,
        "refusal_reasons": list(result.refusal_reasons),
        "entries": [
            {
                "name": entry.name,
                "status": entry.status.value,
                "reasons": list(entry.reasons),
                "partitions": entry.partitions,
            }
            for entry in result.entries
        ],
        "deletes_issued": result.deletes_issued,
        "partitions_removed": result.partitions_removed,
    }
    role = (
        "broker_topic_delete_refused"
        if result.status == "refused"
        else "broker_topic_delete_reported"
    )
    return [
        (topics[role], body),
        (
            topics["broker_topic_delete_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerBrokerTopicDelete, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelTopicDeleteCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [
            (topics["broker_topic_delete_run_failed"], json.dumps(failure).encode())
        ]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = [
    "HandlerBrokerTopicDelete",
    "answer_command",
    "events_of",
    "load_topics",
]
