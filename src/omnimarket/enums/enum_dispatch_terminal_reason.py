# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Terminal-reason taxonomy for dispatched lanes (OMN-17018 / off-rails A13).

"Stopped" is not one state. A deliberate ``TaskStop``, a session-quota kill, a
host-overload refusal, a dependency failure and a timeout all present
identically as ``result: null`` today, which is why the deliberately-stopped
2026-08-27 devpi lane is indistinguishable in the record from the two
2026-08-29 session-quota kills. Blanket redispatch across that set is unsafe.

This module gives the reason a typed value and encodes the recovery policy on
the enum member itself, so ``unknown`` is non-redispatchable *by construction*
rather than by a caller remembering to check.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDispatchTerminalDisposition(StrEnum):
    """How a dispatched lane reached its terminal state.

    ``COMPLETED`` carries no terminal reason — the reason taxonomy describes
    *stops*, not successful completion. ``STOPPED`` always carries one.
    """

    COMPLETED = "completed"
    STOPPED = "stopped"


@unique
class EnumDispatchTerminalReason(StrEnum):
    """Why a dispatched lane stopped before completing its work."""

    #: An operator or orchestrator stopped the lane on purpose (e.g. halting an
    #: org-wide break). Never auto-redispatchable: the stop was the decision.
    DELIBERATE_CANCELLATION = "deliberate_cancellation"
    #: The human driving the session interrupted it.
    USER_STOP = "user_stop"
    #: The session hit a model/plan usage limit and was killed.
    SESSION_QUOTA = "session_quota"
    #: The process disappeared without a recorded stop (crash, host reboot).
    PROCESS_LOSS = "process_loss"
    #: Work the lane depended on failed or was never satisfiable.
    DEPENDENCY_FAILURE = "dependency_failure"
    #: The host refused or shed the lane under load.
    HOST_OVERLOAD = "host_overload"
    #: The lane exceeded its declared wall-clock budget.
    TIMEOUT = "timeout"
    #: The stop could not be classified. Escalates to a human; it must never
    #: default to "retry" and must never default to "healthy".
    UNKNOWN = "unknown"

    @property
    def auto_redispatchable(self) -> bool:
        """Whether recovery policy may redispatch a lane that stopped for this reason.

        Encoded on the member so an unclassifiable stop cannot be retried by a
        caller that forgot to branch. ``DELIBERATE_CANCELLATION`` and
        ``UNKNOWN`` are refused; the rest describe environmental or transient
        stops where the work itself was never rejected.
        """
        return self not in _NON_REDISPATCHABLE_REASONS


#: Reasons for which automatic redispatch is refused. ``UNKNOWN`` because an
#: unclassifiable stop escalates to a human; ``DELIBERATE_CANCELLATION``
#: because redispatching it would undo the decision that stopped the lane.
_NON_REDISPATCHABLE_REASONS: frozenset[EnumDispatchTerminalReason] = frozenset(
    {
        EnumDispatchTerminalReason.DELIBERATE_CANCELLATION,
        EnumDispatchTerminalReason.UNKNOWN,
    }
)


__all__: list[str] = [
    "EnumDispatchTerminalDisposition",
    "EnumDispatchTerminalReason",
]
