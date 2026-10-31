# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold delivery facts and a supplied clock through the contract's FSM."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.models.fsm.model_fsm_transition_result import (
    ModelFSMTransitionResult,
)
from omnibase_core.utils.util_fsm_executor import execute_transition

from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_delivery_path_reducer.models.model_delivery_path import (
    ModelDeliveryConfig,
    ModelDeliveryDecisionRow,
    ModelDeliveryPathEvent,
    ModelDeliveryPathRecord,
    ModelDeliveryPathRequest,
    ModelDeliveryPathResult,
)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def build_context(
    record: ModelDeliveryPathRecord,
    event: ModelDeliveryPathEvent | None,
    config: ModelDeliveryConfig,
) -> dict[str, object]:
    """Build guard facts, failing closed when reference or receipt evidence is absent."""
    reference = event.reference if event is not None else None
    receipt = event.receipt if event is not None else None
    consumer = event.consumer if event is not None else None
    return {
        "ref_kind": reference.ref_kind if reference is not None else "",
        "reference_allowed": (
            reference is not None
            and reference.reference_allowed
            and reference.ref_kind not in config.refused_reference_kinds
        ),
        "receipt_matches_tag": (
            receipt is not None
            and receipt.tag is not None
            and record.target_tag is not None
            and receipt.tag == record.target_tag
        ),
        "receipt_target_current": (
            receipt is not None
            and record.target_tag is not None
            and receipt.target_generation is not None
            and receipt.target_generation == record.target_generation
            and receipt.artifact_identity is not None
            and record.artifact_identity is not None
            and receipt.artifact_identity == record.artifact_identity
        ),
        "consumer_on_tag": (
            consumer is not None
            and consumer.tag is not None
            and consumer.tag == record.target_tag
        ),
        "target_resolved": record.target_tag is not None,
    }


class HandlerDeliveryPath:
    """Apply declared transitions and preserve target identity and deadline across retries."""

    def __init__(self, contract_path: Path = CONTRACT_PATH) -> None:
        self._fsm = ModelFSMSubcontract.model_validate(
            contract_section(contract_path, "state_machine")
        )
        self._config = ModelDeliveryConfig.model_validate(
            contract_section(contract_path, "config", "delivery")
        )

    def _transition(
        self, state: str, trigger: str, context: dict[str, object]
    ) -> ModelFSMTransitionResult:
        coro = execute_transition(
            self._fsm, current_state=state, trigger=trigger, context=context
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()

    def handle(self, request: ModelDeliveryPathRequest) -> ModelDeliveryPathResult:
        prior = request.record.state or self._fsm.initial_state
        state = prior
        record = request.record
        triggers: list[str] = []
        rows: list[ModelDeliveryDecisionRow] = []
        accepted = False
        error: str | None = None
        failed_conditions: tuple[str, ...] = ()

        def finish() -> ModelDeliveryPathResult:
            return ModelDeliveryPathResult(
                path_id=request.path_id,
                prior_state=prior,
                state=state,
                moved=state != prior,
                accepted=accepted,
                record=record.model_copy(update={"state": state}),
                triggers=tuple(triggers),
                decision_rows=tuple(rows),
                error=error,
                failed_conditions=failed_conditions,
                decided_at=request.now,
            )

        def decision_entry(from_state: str) -> None:
            if state == self._config.decision_state and state != from_state:
                rows.append(
                    ModelDeliveryDecisionRow.from_record(
                        request.path_id, from_state, record, self._config, request.now
                    )
                )

        event = request.event
        replay = False
        if event is not None:
            if event.tag is not None and event.tag.tag == record.target_tag:
                if event.tag.artifact_identity != record.artifact_identity:
                    error = f"artifact identity of tag {event.tag.tag} changed"
                    return finish()
                replay = True
            if event.merge is not None and (
                event.merge.merge_commit == record.merge_commit
                or (
                    record.expected_since is not None
                    and event.merge.merged_at <= record.expected_since
                )
            ):
                replay = True
            if not replay:
                try:
                    result = self._transition(
                        state, event.trigger, build_context(record, event, self._config)
                    )
                except ModelOnexError as exc:
                    error = exc.message
                else:
                    if result.success:
                        from_state = state
                        state = result.new_state
                        accepted = True
                        triggers.append(event.trigger)
                        if event.tag is not None:
                            record = record.model_copy(
                                update={
                                    "target_tag": event.tag.tag,
                                    "artifact_identity": event.tag.artifact_identity,
                                    "target_generation": record.target_generation + 1,
                                }
                            )
                            if (
                                record.expected_since is None
                                or event.tag.merge.merged_at > record.expected_since
                            ):
                                record = record.model_copy(
                                    update={
                                        "expected_since": event.tag.merge.merged_at,
                                        "merge_commit": event.tag.merge.merge_commit,
                                        "stale_decided_for": None,
                                    }
                                )
                        if event.merge is not None and (
                            record.expected_since is None
                            or event.merge.merged_at > record.expected_since
                        ):
                            record = record.model_copy(
                                update={
                                    "expected_since": event.merge.merged_at,
                                    "merge_commit": event.merge.merge_commit,
                                    "target_tag": None,
                                    "artifact_identity": None,
                                    "target_generation": record.target_generation + 1,
                                    "stale_decided_for": None,
                                }
                            )
                        if record.expected_since is None:
                            record = record.model_copy(
                                update={"expected_since": request.now}
                            )
                        record = record.model_copy(update={"state": state})
                        decision_entry(from_state)
                    else:
                        error = result.error or "transition refused"
                        failed_conditions = tuple(result.failed_conditions or ())

        if (
            record.expected_since is not None
            and request.now - record.expected_since
            >= timedelta(minutes=self._config.stale_after_minutes)
            and record.stale_decided_for != record.expected_since
        ):
            try:
                result = self._transition(state, self._config.limit_trigger, {})
            except ModelOnexError:
                pass
            else:
                if result.success:
                    from_state = state
                    state = result.new_state
                    triggers.append(self._config.limit_trigger)
                    record = record.model_copy(
                        update={
                            "state": state,
                            "stale_decided_for": record.expected_since,
                        }
                    )
                    decision_entry(from_state)
        return finish()
