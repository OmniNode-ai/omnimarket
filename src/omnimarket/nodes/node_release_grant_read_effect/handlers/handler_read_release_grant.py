# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a head-bound release grant from change history and record the decision."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.models.change_source.model_change_history import (
    EnumAccountType,
    EnumChangeEventKind,
    ModelChangeEvent,
)
from omnimarket.models.release_gate.model_release_grant_facts import (
    EnumGrantState,
    ModelReleaseGrantFacts,
)
from omnimarket.nodes.node_release_grant_read_effect.models.model_release_grant import (
    ModelReadReleaseGrantRequest,
)
from omnimarket.protocols.protocol_change_source import ProtocolChangeSource
from omnimarket.protocols.protocol_clock import ProtocolClock
from omnimarket.protocols.protocol_work_ledger_store import ProtocolWorkLedgerStore

type _Decision = tuple[EnumGrantState, str, str | None, str | None]


def _read_decision(
    request: ModelReadReleaseGrantRequest, source: ProtocolChangeSource
) -> _Decision:
    try:
        history = source.change_history(request.repository, request.pull_request)
    except Exception:
        return EnumGrantState.BLOCKED, "label_history_unreadable", None, None
    if (history.repository, history.pull_request) != (
        request.repository,
        request.pull_request,
    ):
        return EnumGrantState.BLOCKED, "history_for_another_change", None, None
    events = sorted(enumerate(history.events), key=lambda item: (item[1].at, item[0]))
    pushes = [
        (index, event)
        for index, event in events
        if event.kind is EnumChangeEventKind.PUSHED
    ]
    matching = [
        (index, event)
        for index, event in pushes
        if event.head_sha is not None
        and event.head_sha.casefold() == request.head_sha.casefold()
    ]
    if not matching:
        return EnumGrantState.BLOCKED, "head_push_not_in_history", None, None
    push_index, push = matching[-1]
    if any(
        (event.at, index) > (push.at, push_index)
        and (event.head_sha or "").casefold() != request.head_sha.casefold()
        for index, event in pushes
    ):
        return EnumGrantState.BLOCKED, "head_is_not_current", None, None
    applied: ModelChangeEvent | None = None
    was_removed = False
    for _, event in events:
        if (event.label or "").casefold() != request.policy.grant_label.casefold():
            continue
        if event.kind is EnumChangeEventKind.LABELED:
            applied = event
        elif event.kind is EnumChangeEventKind.UNLABELED:
            applied = None
            was_removed = True
    if applied is None:
        reason = "label_removed" if was_removed else "no_grant_label"
    elif applied.at <= push.at:
        reason = "label_before_head_push"
    elif applied.actor_type is not EnumAccountType.USER or applied.via_app:
        reason = "applier_not_a_person"
    elif applied.actor_login.casefold() in {
        identity.casefold() for identity in request.policy.agent_identities
    }:
        reason = "applier_is_agent"
    else:
        for role in request.policy.grantor_roles:
            if applied.actor_login.casefold() in {
                login.casefold() for login in request.policy.role_logins[role]
            }:
                return EnumGrantState.GRANTED, "granted", applied.event_id, role
        reason = "applier_not_in_role_list"
    return EnumGrantState.NOT_GRANTED, reason, None, None


def _decision_fields(facts: ModelReleaseGrantFacts) -> dict[str, str]:
    return {
        "boundary": facts.boundary.value,
        "repository": facts.repository,
        "pull_request": str(facts.pull_request),
        "head_sha": facts.head_sha,
        "state": facts.state.value,
        "reason": facts.reason,
        "grant_event_id": facts.grant_event_id or "",
        "applier_role": facts.applier_role or "",
        "policy_digest": facts.policy_digest,
        "read_at": facts.read_at.isoformat(),
    }


class HandlerReadReleaseGrant:
    """Read historical authorization through injected change, ledger and clock ports."""

    def __init__(
        self,
        change_source: ProtocolChangeSource | None = None,
        ledger: ProtocolWorkLedgerStore | None = None,
        clock: ProtocolClock | None = None,
    ) -> None:
        self._change_source = change_source
        self._ledger = ledger
        self._clock = clock

    def handle(self, request: ModelReadReleaseGrantRequest) -> ModelReleaseGrantFacts:
        """Read the grant fresh; every failure is a blocked state, never an admission."""
        source, ledger, clock = self._change_source, self._ledger, self._clock
        digest = request.policy.digest()
        if source is None or ledger is None or clock is None:
            return self._facts(
                request,
                digest,
                datetime.min.replace(tzinfo=UTC) if clock is None else clock.now(),
                (EnumGrantState.BLOCKED, "ports_not_bound", None, None),
            )
        facts = self._facts(
            request, digest, clock.now(), _read_decision(request, source)
        )
        try:
            ledger.append_row("release_gate_decision", _decision_fields(facts))
        except Exception:
            if facts.state is EnumGrantState.GRANTED:
                return facts.model_copy(
                    update={
                        "state": EnumGrantState.BLOCKED,
                        "reason": "decision_row_unwritable",
                    }
                )
        return facts

    @staticmethod
    def _facts(
        request: ModelReadReleaseGrantRequest,
        digest: str,
        now: datetime,
        decision: _Decision,
    ) -> ModelReleaseGrantFacts:
        state, reason, event_id, role = decision
        return ModelReleaseGrantFacts(
            repository=request.repository,
            pull_request=request.pull_request,
            head_sha=request.head_sha,
            boundary=request.boundary,
            state=state,
            reason=reason,
            grant_event_id=event_id,
            applier_role=role,
            policy_digest=digest,
            read_at=now,
        )
