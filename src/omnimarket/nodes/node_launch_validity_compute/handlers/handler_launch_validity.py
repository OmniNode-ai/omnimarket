# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether a lane's target is still valid at the moment of launch.

Pure: the caller's reads and the policy are the whole input. Which reads a launch needs,
the age limit, the open state, the done states and the mode are all in the policy; this
handler holds none of them. A read that is absent, failed or older than the limit refuses
and names the read.
"""

from __future__ import annotations

from collections.abc import Callable

from ..models.model_launch_validity import (
    EnumLaunchValidityMode,
    EnumRead,
    EnumRefusalCode,
    ModelLaunchTarget,
    ModelLaunchValidityPolicy,
    ModelLaunchValidityRequest,
    ModelLaunchValidityResult,
    ModelRefusal,
    ModelStateRead,
    ModelWouldRefuseRecord,
)

_Rule = Callable[
    [ModelLaunchTarget, ModelStateRead, ModelLaunchValidityPolicy], list[ModelRefusal]
]


def _refusal(code: EnumRefusalCode, read: EnumRead, detail: str) -> ModelRefusal:
    return ModelRefusal(code=code, read=read, detail=detail)


def _head_matches(expected: str, got: str, min_chars: int) -> bool:
    shorter = min(len(expected), len(got))
    return shorter >= min_chars and expected[:shorter] == got[:shorter]


def _pr_rule(
    target: ModelLaunchTarget, read: ModelStateRead, policy: ModelLaunchValidityPolicy
) -> list[ModelRefusal]:
    found: list[ModelRefusal] = []
    state = read.state or ""
    if state != policy.pr_open_state:
        code = policy.pr_state_codes.get(state, EnumRefusalCode.PR_NOT_OPEN)
        found.append(
            _refusal(code, EnumRead.PR, f"{target.pr} state={state or 'unread'}")
        )
    pr_class = read.pr_class or ""
    if pr_class in policy.pr_class_codes:
        found.append(
            _refusal(
                policy.pr_class_codes[pr_class],
                EnumRead.PR,
                f"{target.pr} class={pr_class}",
            )
        )
    if target.expected_head and not _head_matches(
        target.expected_head, read.head or "", policy.min_head_chars
    ):
        found.append(
            _refusal(
                EnumRefusalCode.PR_HEAD_MISMATCH,
                EnumRead.PR,
                f"{target.pr} head={read.head or 'unread'} expected={target.expected_head}",
            )
        )
    return found


def _done_rule(
    code: EnumRefusalCode,
    read_name: EnumRead,
    subject: Callable[[ModelLaunchTarget], str | None],
) -> _Rule:
    def rule(
        target: ModelLaunchTarget,
        read: ModelStateRead,
        policy: ModelLaunchValidityPolicy,
    ) -> list[ModelRefusal]:
        found: list[ModelRefusal] = []
        if read.state is not None and read.state in policy.done_states:
            found.append(
                _refusal(code, read_name, f"{subject(target)} state={read.state}")
            )
        if read.done_by:
            found.append(
                _refusal(
                    code,
                    read_name,
                    f"{subject(target)} done by merged {', '.join(read.done_by)}",
                )
            )
        return found

    return rule


def _owner_rule(
    target: ModelLaunchTarget, read: ModelStateRead, policy: ModelLaunchValidityPolicy
) -> list[ModelRefusal]:
    del policy
    others = tuple(lane for lane in read.owners if lane != target.lane)
    if not others:
        return []
    return [
        _refusal(
            EnumRefusalCode.LIVE_OWNER,
            EnumRead.OWNER,
            f"live CLAIM held by {', '.join(others)}",
        )
    ]


_RULES: dict[EnumRead, _Rule] = {
    EnumRead.PR: _pr_rule,
    EnumRead.TICKET: _done_rule(
        EnumRefusalCode.TICKET_DONE, EnumRead.TICKET, lambda t: t.ticket
    ),
    EnumRead.PLAN_TASK: _done_rule(
        EnumRefusalCode.PLAN_TASK_DONE, EnumRead.PLAN_TASK, lambda t: t.plan_task
    ),
    EnumRead.OWNER: _owner_rule,
}


class HandlerLaunchValidity:
    """``handle(ModelLaunchValidityRequest) -> ModelLaunchValidityResult``."""

    def handle(self, request: ModelLaunchValidityRequest) -> ModelLaunchValidityResult:
        policy, target = request.policy, request.target
        refusals: list[ModelRefusal] = []
        for need in policy.reads:
            if not getattr(target, need.target_field, None):
                continue
            read = request.reads.get(need.read)
            if read is None:
                refusals.append(
                    _refusal(
                        EnumRefusalCode.READ_MISSING,
                        need.read,
                        f"the {need.read} read was not made",
                    )
                )
            elif not read.ok:
                refusals.append(
                    _refusal(
                        EnumRefusalCode.READ_FAILED,
                        need.read,
                        f"the {need.read} read failed: {read.detail or 'no detail'}",
                    )
                )
            elif (
                read.age_seconds is None
                or read.age_seconds > policy.max_read_age_seconds
            ):
                refusals.append(
                    _refusal(
                        EnumRefusalCode.READ_STALE,
                        need.read,
                        f"the {need.read} read is {read.age_seconds if read.age_seconds is not None else 'of unknown'} "
                        f"seconds old; the limit is {policy.max_read_age_seconds}",
                    )
                )
            else:
                refusals.extend(_RULES[need.read](target, read, policy))
        report_only = policy.mode is EnumLaunchValidityMode.REPORT_ONLY
        return ModelLaunchValidityResult(
            lane=target.lane,
            mode=policy.mode,
            refusals=tuple(refusals),
            blocks=bool(refusals) and not report_only,
            would_refuse=(
                ModelWouldRefuseRecord(
                    lane=target.lane,
                    ticket=target.ticket,
                    pr=target.pr,
                    refusals=tuple(refusals),
                )
                if refusals and report_only
                else None
            ),
        )
