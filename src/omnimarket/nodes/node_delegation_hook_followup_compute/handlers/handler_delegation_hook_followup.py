# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B hook follow-up handler, with no I/O or retained state.

Ported from the omni queue-status reader's pure ``fold``, ``Agent.retries``,
``attribute`` and ``find_repeats``. Call pairing, refusal precedence and retry
semantics are preserved. Attribution uses the receipt's exact identity/digest
or its bounded call window instead of scoring prose ledger rows. The rare-work
repeat threshold remains five shared digests seen in at most four agents.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Literal

from omnimarket.nodes.node_delegation_hook_followup_compute.models.model_hook_followup_request import (
    ModelHookEvent,
    ModelHookFollowupRequest,
    ModelHookReceipt,
)
from omnimarket.nodes.node_delegation_hook_followup_compute.models.model_hook_followup_result import (
    ModelHookFollowupResult,
    ModelHookFollowupSignal,
    ModelHookReceiptFollowup,
)

AgentKey = tuple[str, str | None]


@dataclass
class _Call:
    tool: str
    sha: str | None
    start: int
    tool_use_id: str
    end: int | None = None
    outcome: str = "pending"


def _event_order(event: ModelHookEvent) -> tuple[int, int, int, str]:
    # Canonical tie-breaks make a supplied tuple's order immaterial even when
    # an export has no cursor. PermissionDenied retains precedence in fold.
    phase = {
        "PreToolUse": 0,
        "PostToolUse": 1,
        "PostToolUseFailure": 2,
        "PermissionDenied": 3,
    }.get(event.hook_event_name, 4)
    return event.timestamp_ms, event.cursor, phase, event.model_dump_json()


def fold(events: tuple[ModelHookEvent, ...]) -> dict[AgentKey, list[_Call]]:
    """Pair calls by (session, agent, tool_use_id), as in the hook reader."""
    agents: dict[AgentKey, list[_Call]] = {}
    open_calls: dict[tuple[str, str | None, str], _Call] = {}
    call: _Call | None
    for event in sorted(set(events), key=_event_order):
        key = (event.session_id, event.agent_id)
        calls = agents.setdefault(key, [])
        if not event.tool_use_id:
            continue
        call_key = (*key, event.tool_use_id)
        if event.hook_event_name == "PreToolUse":
            call = _Call(
                event.tool_name or "?",
                event.tool_input_sha256,
                event.timestamp_ms,
                event.tool_use_id,
            )
            open_calls[call_key] = call
            calls.append(call)
        elif event.hook_event_name in (
            "PostToolUse",
            "PostToolUseFailure",
            "PermissionDenied",
        ):
            call = open_calls.get(call_key)
            if call is None:
                # The Pre may precede capture: preserve the reader's
                # zero-length completion instead of inventing an input hash.
                call = _Call(
                    event.tool_name or "?", None, event.timestamp_ms, event.tool_use_id
                )
                open_calls[call_key] = call
                calls.append(call)
            if event.hook_event_name == "PermissionDenied":
                call.outcome = "refused"
            elif call.outcome != "refused":
                call.outcome = (
                    "failed" if event.hook_event_name == "PostToolUseFailure" else "ok"
                )
            call.end = event.timestamp_ms
            if event.tool_name and call.tool == "?":
                call.tool = event.tool_name
    return agents


def _identity_matches(receipt: ModelHookReceipt, event: ModelHookEvent) -> bool:
    return (
        receipt.hook_session_id is None or event.session_id == receipt.hook_session_id
    ) and (receipt.hook_agent_id is None or event.agent_id == receipt.hook_agent_id)


def attribute(
    receipt: ModelHookReceipt, events: tuple[ModelHookEvent, ...]
) -> tuple[Literal["digest", "time", "ambiguous", "none"], AgentKey | None]:
    """Never guess an agent: exact call first, unique time-window agent second."""
    window = tuple(
        event
        for event in events
        if receipt.started_at_ms <= event.timestamp_ms <= receipt.ended_at_ms
        and _identity_matches(receipt, event)
    )
    digest = receipt.tool_input_sha256 or receipt.command_sha256
    # PreToolUse fires before the delegate process records started_at_ms.
    # Accept an earlier Pre only when its paired completion proves overlap;
    # an old pending call must not accidentally become an exact join.
    matching_events = tuple(
        event for event in events if _identity_matches(receipt, event)
    )
    exact = {
        (session, agent, call.tool_use_id)
        for (session, agent), calls in fold(matching_events).items()
        for call in calls
        if digest is not None
        and call.tool == "Bash"
        and call.sha == digest
        and call.start <= receipt.ended_at_ms
        and (
            call.start >= receipt.started_at_ms
            or (call.end is not None and call.end >= receipt.started_at_ms)
        )
    }
    if len(exact) > 1:
        return "ambiguous", None
    if exact:
        session, agent, _ = next(iter(exact))
        return "digest", (session, agent)
    candidates = {(event.session_id, event.agent_id) for event in window}
    if len(candidates) > 1:
        return "ambiguous", None
    if candidates:
        return "time", next(iter(candidates))
    return "none", None


def _retries(calls: list[_Call]) -> int:
    bad: set[tuple[str, str]] = set()
    count = 0
    for call in sorted(
        (call for call in calls if call.outcome != "pending"),
        key=lambda call: call.end if call.end is not None else call.start,
    ):
        if call.sha is None:
            continue
        key = (call.tool, call.sha)
        if key in bad:
            count += 1
        if call.outcome in ("failed", "refused"):
            bad.add(key)
    return count


def find_repeats(
    agents: dict[AgentKey, list[_Call]], active: AgentKey
) -> tuple[str, ...]:
    """Port of find_repeats' rare completed-work digest comparison.

    Stop/prompt repeats are outside this signal: the input and result expose
    tool-input digests only. Main sessions remain excluded from work pairs.
    """
    digests = {
        key: {call.sha for call in calls if call.sha and call.outcome != "pending"}
        for key, calls in agents.items()
        if key[1] is not None
    }
    seen_in: Counter[str] = Counter()
    for shas in digests.values():
        seen_in.update(shas)
    rare = {
        key: {sha for sha in shas if seen_in[sha] <= 4} for key, shas in digests.items()
    }
    own = rare.get(active, set())
    return tuple(
        sorted(
            f"{session}/{agent}"
            for (session, agent), shas in rare.items()
            if (session, agent) != active and len(own & shas) >= 5
        )
    )


class HandlerDelegationHookFollowup:
    def handle(self, request: ModelHookFollowupRequest) -> ModelHookFollowupResult:
        """Return one observation per receipt, in the caller's receipt order."""
        events = tuple(
            event
            for event in request.hook_events
            if request.window_start_ms <= event.timestamp_ms <= request.window_end_ms
        )
        return ModelHookFollowupResult(
            followups=tuple(
                self._followup(receipt, events, request) for receipt in request.receipts
            )
        )

    def _followup(
        self,
        receipt: ModelHookReceipt,
        events: tuple[ModelHookEvent, ...],
        request: ModelHookFollowupRequest,
    ) -> ModelHookReceiptFollowup:
        join, agent = attribute(receipt, events)
        empty = ModelHookReceiptFollowup(
            receipt_key=receipt.receipt_key, join=join, signal=None
        )
        if agent is None:
            return empty
        terminals = tuple(
            row
            for row in request.lane_rows
            if row.kind == "TERMINAL"
            and row.lane == receipt.lane
            and row.timestamp_ms >= receipt.ended_at_ms
        )
        if not terminals:
            return empty
        end = min(row.timestamp_ms for row in terminals)
        if (
            request.window_start_ms > receipt.started_at_ms
            or request.window_end_ms < end
        ):
            return empty
        terminal_prs = {
            row.pr for row in terminals if row.timestamp_ms == end and row.pr
        }
        if len(terminal_prs) > 1:
            return ModelHookReceiptFollowup(
                receipt_key=receipt.receipt_key, join="ambiguous", signal=None
            )
        # Fold with pre-window context, then select calls that started after
        # the answer. This excludes the delegate command's late completion.
        folded = fold(tuple(event for event in events if event.timestamp_ms <= end))
        after = {
            key: [call for call in calls if receipt.ended_at_ms <= call.start <= end]
            for key, calls in folded.items()
        }
        calls = after.get(agent, [])
        counts = Counter(call.sha for call in calls if call.sha)
        pr = receipt.pr or next(iter(terminal_prs), None)
        peer_fix = pr is not None and any(
            row.kind == "CLAIM"
            and row.action == "fix"
            and row.pr == pr
            and row.lane != receipt.lane
            and end < row.timestamp_ms <= request.window_end_ms
            for row in request.lane_rows
        )
        signal = ModelHookFollowupSignal(
            session_id=agent[0],
            agent_id=agent[1],
            window_start_ms=receipt.ended_at_ms,
            window_end_ms=end,
            # PreToolUse proves a call even if its completion is still pending.
            tool_calls=len(calls),
            failures=sum(call.outcome == "failed" for call in calls),
            refusals=sum(call.outcome == "refused" for call in calls),
            retries=_retries(calls),
            repeated_digests=tuple(
                sorted(sha for sha, count in counts.items() if count > 1)
            ),
            repeated_work_agents=find_repeats(after, agent),
            edit_count=sum(call.tool == "Edit" for call in calls),
            write_count=sum(call.tool == "Write" for call in calls),
            peer_lane_returned_for_fix=peer_fix,
        )
        return ModelHookReceiptFollowup(
            receipt_key=receipt.receipt_key, join=join, signal=signal
        )
