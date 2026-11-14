# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Revision-five BatchProbe fold, fenced with the unique slot claims in one transaction."""

from collections import Counter

from omnimarket.handlers.container.container_overlay_dependencies import (
    require_dependencies,
)
from omnimarket.models.model_rule_premise import (
    ModelBatchPolicy,
    ModelBatchRequest,
    ModelBatchResult,
    ModelBatchSlot,
    ModelBatchState,
)
from omnimarket.protocols.protocol_rule_premise import (
    ProtocolBatchClaimStore,
    ProtocolPremiseValidator,
)


def _held(state: ModelBatchState, now: float, waiting: bool) -> str:
    if state.held:
        return state.held
    classes = Counter(
        slot.class_key
        for slot in state.slots
        if slot.state == "blocked"
        and slot.class_key
        and 0 <= now - slot.blocked_at <= state.policy.window_s
    )
    if any(n >= state.policy.blocked_threshold for n in classes.values()):
        return "same-class-threshold"
    capacity = state.policy.probe + len(state.proofs) * state.policy.step
    # A retry belongs to its original brief; it is not a spent slot yet.
    if (
        waiting
        and len(state.slots) >= capacity
        and all(
            slot.state in {"blocked", "expired", "timed-out", "proved"}
            for slot in state.slots
        )
    ):
        return "slots-exhausted"
    return ""


class HandlerBatchProbe:
    def __init__(
        self,
        store: ProtocolBatchClaimStore | None = None,
        policy: ModelBatchPolicy | None = None,
        validator: ProtocolPremiseValidator | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._validator = validator

    def handle(self, request: ModelBatchRequest) -> ModelBatchResult:
        try:
            if self._store is None or self._validator is None:
                from .handler_rule_premise import VALIDATOR

                dependencies = require_dependencies(
                    [
                        "omnimarket.protocols.protocol_rule_premise.ProtocolBatchClaimStore",
                        VALIDATOR,
                    ]
                )
                store = dependencies[
                    "omnimarket.protocols.protocol_rule_premise.ProtocolBatchClaimStore"
                ]
                validator = dependencies[VALIDATOR]
                if not isinstance(store, ProtocolBatchClaimStore) or not isinstance(
                    validator, ProtocolPremiseValidator
                ):
                    raise TypeError(
                        "batch claim or core validator binding has the wrong protocol"
                    )
                self._store, self._validator = store, validator
            if self._policy is None:
                from .premise_policy import load_batch_policy

                self._policy = load_batch_policy()
            with self._store.transaction(request.batch) as tx:
                state = tx.read() or ModelBatchState(
                    batch=request.batch, policy=self._policy
                )
                if state.policy != self._policy:
                    return ModelBatchResult(
                        verdict="refuse", reason="batch-policy-changed"
                    )
                slots = list(state.slots)
                for i, slot in enumerate(slots):
                    if slot.state == "running" and request.now >= slot.lease_until:
                        slots[i] = slot.model_copy(
                            update={
                                "state": "retry"
                                if slot.attempt < state.policy.max_attempts
                                else "expired"
                            }
                        )
                state = state.model_copy(update={"slots": tuple(slots)})
                # Admission itself reads the threshold before accepting any start or proof.
                reason = _held(state, request.now, request.work_waiting)
                if reason:
                    state = state.model_copy(
                        update={
                            "held": reason,
                            "escalation": state.escalation
                            or f"batch@{request.batch}:held",
                        }
                    )
                    tx.write(state)
                    return self._result(state, "held", reason=reason)

                if request.operation == "admit":
                    existing = next(
                        (s for s in slots if s.brief_id == request.brief_id), None
                    )
                    if existing is not None:
                        if existing.premise != request.premise:
                            return self._result(
                                state, "refuse", reason="brief-premise-changed"
                            )
                        if existing.state != "retry":
                            tx.write(state)
                            return self._result(
                                state, "wait", reason="brief-already-claimed"
                            )
                        slot = existing.model_copy(
                            update={
                                "attempt": existing.attempt + 1,
                                "state": "running",
                                "lease_until": request.now + state.policy.lease_s,
                            }
                        )
                        slots[slots.index(existing)] = slot
                    else:
                        capacity = (
                            state.policy.probe + len(state.proofs) * state.policy.step
                        )
                        if len(slots) >= capacity:
                            tx.write(state)
                            return self._result(
                                state, "wait", reason="probe-waiting-for-proof"
                            )
                        slot = ModelBatchSlot(
                            key=f"batch@{request.batch}#{len(slots) + 1}",
                            brief_id=request.brief_id,
                            premise=request.premise,
                            attempt=1,
                            lease_until=request.now + state.policy.lease_s,
                            state="running",
                        )
                        if not tx.insert_slot(slot):
                            raise RuntimeError("unique slot claim conflict")
                        slots.append(slot)
                    state = state.model_copy(update={"slots": tuple(slots)})
                    tx.write(state)
                    return self._result(
                        state, "admit", slot=slot.key, attempt=slot.attempt
                    )

                index = next(
                    (
                        i
                        for i, s in enumerate(slots)
                        if s.key == request.slot
                        and s.brief_id == request.brief_id
                        and s.premise == request.premise
                        and s.attempt == request.attempt
                    ),
                    None,
                )
                if index is None:
                    tx.write(state)
                    return self._result(
                        state, "refuse", reason="stale-or-wrong-slot-fence"
                    )
                slot = slots[index]
                if request.operation == "checkpoint":
                    tx.write(state)
                    return self._result(
                        state, "recorded", reason="checkpoint-is-not-proof"
                    )
                if request.operation == "proof":
                    if slot.state != "running" or not self._validator.read_back(
                        request.receipt_id, request.premise, request.brief_id
                    ):
                        tx.write(state)
                        return self._result(
                            state, "refuse", reason="unvalidated-premise-read-back"
                        )
                    if request.receipt_id not in state.proofs:
                        state = state.model_copy(
                            update={"proofs": (*state.proofs, request.receipt_id)}
                        )
                    slots[index] = slot.model_copy(update={"state": "proved"})
                elif slot.state != "running":
                    tx.write(state)
                    return self._result(state, "refuse", reason="slot-already-settled")
                elif request.operation == "crashed":
                    # Retain the claim until the lease expires; no immediate replacement.
                    tx.write(state)
                    return self._result(
                        state, "recorded", reason="crash-waits-for-lease"
                    )
                else:
                    slots[index] = slot.model_copy(
                        update={
                            "state": "blocked"
                            if request.operation == "blocked"
                            else "timed-out",
                            "class_key": request.class_key,
                            "blocked_at": request.now,
                        }
                    )
                state = state.model_copy(update={"slots": tuple(slots)})
                reason = _held(state, request.now, request.work_waiting)
                if reason:
                    state = state.model_copy(
                        update={
                            "held": reason,
                            "escalation": state.escalation
                            or f"batch@{request.batch}:held",
                        }
                    )
                tx.write(state)
                return self._result(
                    state, "held" if state.held else "recorded", reason=reason
                )
        except (
            OSError,
            ValueError,
            TypeError,
            LookupError,
            RuntimeError,
            ImportError,
            AttributeError,
        ) as exc:
            return ModelBatchResult(
                verdict="UNREAD", reason=f"batch claim store: {exc}"
            )

    @staticmethod
    def _result(
        state: ModelBatchState, verdict: str, **values: object
    ) -> ModelBatchResult:
        return ModelBatchResult.model_validate(
            {
                "verdict": verdict,
                "escalation": state.escalation,
                "capacity": state.policy.probe + len(state.proofs) * state.policy.step,
                **values,
            }
        )
