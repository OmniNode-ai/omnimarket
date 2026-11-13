# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure S8 decision over a complete normalized ledger snapshot.

All policy words and row shapes come from the overlay. No ledger read, clock,
dispatch or validation implementation is hidden in this handler.
"""

import base64
import hashlib
import json
from importlib import resources

import yaml

from omnimarket.models.model_stop_only_on_withdrawal import (
    ModelS8Claim,
    ModelS8Cursor,
    ModelS8Flag,
    ModelS8Hold,
    ModelS8LedgerWrite,
    ModelS8Message,
    ModelS8Policy,
    ModelS8Release,
    ModelS8Repair,
    ModelS8Request,
    ModelS8Result,
    ModelS8ScopeByTask,
    ModelS8Terminal,
)


def _key(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def load_s8_policy() -> ModelS8Policy:
    return ModelS8Policy.model_validate(
        yaml.safe_load(
            resources.files("omnimarket.nodes.node_stop_only_on_withdrawal_compute")
            .joinpath("overlay.yaml")
            .read_text()
        )
    )


def _same_scope(left: ModelS8ScopeByTask, right: ModelS8ScopeByTask) -> bool:
    return all(
        getattr(left, name) == getattr(right, name)
        for name in ModelS8ScopeByTask.model_fields
    )


class HandlerStopOnlyOnWithdrawal:
    def __init__(self, policy: ModelS8Policy | None = None) -> None:
        self._policy = policy or load_s8_policy()

    def _withdrawal(
        self, message: ModelS8Message, claim: ModelS8Claim, terminal: ModelS8Terminal
    ) -> bool:
        return (
            message.effect == self._policy.withdrawal_effect
            and message.task_id == terminal.task_id
            and message.revision == terminal.revision
            and bool({terminal.lane, terminal.task_id}.intersection(message.targets))
            and claim.sequence < message.sequence < terminal.sequence
            and claim.stamp <= message.stamp <= terminal.stamp
        )

    def handle(self, request: ModelS8Request) -> ModelS8Result:
        snapshot, policy, previous = request.snapshot, self._policy, request.cursor
        if not snapshot.complete:
            raise ValueError("S8 needs complete CLAIM, MSG, HOLD and RELEASE history")
        if previous is not None and (
            previous.ledger_id != request.ledger_id
            or previous.check_id != policy.check_id
            or previous.through_sequence > snapshot.through_sequence
        ):
            raise ValueError("S8 cursor does not belong to this snapshot")
        facts: tuple[
            ModelS8Claim | ModelS8Message | ModelS8Hold | ModelS8Release, ...
        ] = (
            *snapshot.claims,
            *snapshot.messages,
            *snapshot.holds,
            *snapshot.releases,
            *snapshot.terminals,
        )
        positions = [fact.sequence for fact in facts]
        if len(positions) != len(set(positions)) or any(
            fact.sequence > snapshot.through_sequence or fact.stamp > request.tick_at
            for fact in facts
        ):
            raise ValueError("S8 snapshot positions or timestamps are inconsistent")
        flagged = set(previous.flagged_terminal_keys if previous else ())
        repaired = set(snapshot.repaired_keys)
        repaired.update(previous.repaired_keys if previous else ())
        flags: list[ModelS8Flag] = []
        repairs: list[ModelS8Repair] = []
        writes: list[ModelS8LedgerWrite] = []
        for scope in snapshot.scopes:
            claims = [
                claim
                for claim in snapshot.claims
                if claim.task_id == scope.task_id and claim.lane == scope.lane
            ]
            if not claims or not _same_scope(
                claim := max(claims, key=lambda fact: fact.sequence), scope
            ):
                raise ValueError("S8 scope must name the lane's current CLAIM run")
            for terminal in sorted(snapshot.terminals, key=lambda fact: fact.sequence):
                if not _same_scope(terminal, scope):
                    continue
                if terminal.sequence <= claim.sequence or terminal.stamp < claim.stamp:
                    raise ValueError("S8 TERMINAL precedes its CLAIM")
                terminal_key = _key(request.ledger_id, str(terminal.sequence))
                if terminal_key in flagged:
                    continue
                withdrawal = [
                    message
                    for message in snapshot.messages
                    if self._withdrawal(message, claim, terminal)
                ]
                reason = None
                if terminal.outcome == policy.withdrawn_outcome:
                    cited = [
                        message
                        for message in snapshot.messages
                        if terminal.withdrawn_by
                        in (
                            message.message_id,
                            message.stamp.isoformat().replace("+00:00", "Z"),
                        )
                    ]
                    if len(cited) != 1 or cited[0] not in withdrawal:
                        reason = "invalid-withdrawal-citation"
                elif terminal.outcome in policy.fallback_outcomes and any(
                    phrase.casefold()
                    in f"{terminal.blocker}\n{terminal.report}".casefold()
                    for phrase in policy.operator_stop_phrases
                ):
                    active_hold = any(
                        hold.sequence < terminal.sequence
                        and hold.stamp <= terminal.stamp
                        and bool(
                            {terminal.lane, terminal.task_id}.intersection(hold.targets)
                        )
                        and hold.task_id in (None, terminal.task_id)
                        and hold.revision in (None, terminal.revision)
                        and not any(
                            release.hold_id == hold.hold_id
                            and release.sequence > hold.sequence
                            for release in snapshot.releases
                        )
                        for hold in snapshot.holds
                    )
                    if not withdrawal and not active_hold:
                        reason = "operator-stop-without-authority"
                if reason is None:
                    continue
                flag = ModelS8Flag(
                    check_id=policy.check_id,
                    terminal_sequence=terminal.sequence,
                    scope=scope,
                    reason=reason,
                )
                flags.append(flag)
                flagged.add(terminal_key)
                repair_key = _key(request.ledger_id, scope.task_id, scope.revision)
                if repair_key in repaired:
                    continue
                repaired.add(repair_key)
                repair = ModelS8Repair(
                    action_id=f"{policy.check_id}:{repair_key}",
                    scope=scope,
                    terminal_sequence=terminal.sequence,
                )
                repairs.append(repair)
                writes.append(
                    ModelS8LedgerWrite(
                        row_id=repair.action_id,
                        row=policy.repair_row_template.format(
                            stamp=request.tick_at.isoformat().replace("+00:00", "Z"),
                            lane=policy.observer_lane,
                            check=policy.check_id,
                            action_id=repair.action_id,
                            task=scope.task_id,
                            revision=scope.revision,
                            stopped_lane=scope.lane,
                            run=scope.run_id,
                            terminal=terminal.sequence,
                            repair_key=repair_key,
                        ),
                    )
                )
        cursor = ModelS8Cursor(
            check_id=policy.check_id,
            ledger_id=request.ledger_id,
            through_sequence=snapshot.through_sequence,
            flagged_terminal_keys=tuple(sorted(flagged)),
            repaired_keys=tuple(sorted(repaired)),
        )
        cursor_id = f"{policy.check_id}:{_key(request.ledger_id, request.tick_id, cursor.model_dump_json())}"
        encoded_cursor = base64.urlsafe_b64encode(
            cursor.model_dump_json().encode()
        ).decode()
        return ModelS8Result(
            flags=tuple(flags),
            repairs=tuple(repairs),
            repair_writes=tuple(writes),
            cursor=cursor,
            cursor_write=ModelS8LedgerWrite(
                row_id=cursor_id,
                row=policy.cursor_row_template.format(
                    stamp=request.tick_at.isoformat().replace("+00:00", "Z"),
                    lane=policy.observer_lane,
                    check=policy.check_id,
                    cursor_id=cursor_id,
                    cursor=snapshot.through_sequence,
                    encoded_cursor=encoded_cursor,
                    tick=request.tick_id,
                ),
            ),
        )
