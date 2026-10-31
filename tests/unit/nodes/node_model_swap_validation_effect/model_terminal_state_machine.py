# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Finite transition model of durable validation ownership and terminal delivery.

The provider boundary is invoked only after start intent is committed. A crash
before that boundary may acquire a new fence; a crash after it records an unknown remote
outcome as a timeout and never resubmits the request. Duplicate delivery observes
state or republishes the immutable terminal; it never creates another run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class Stage(StrEnum):
    REQUESTED = "requested"
    CLAIMED = "ownership_claimed"
    LOADED = "request_validated"
    INTENT = "start_intent_stored"
    STARTED = "service_request_started"
    REPORT = "terminal_received"
    STORED = "terminal_stored"
    PUBLISHED = "terminal_published"


@dataclass(frozen=True)
class State:
    stage: Stage = Stage.REQUESTED
    alive: bool = True
    fence: int = 0
    starts: int = 0
    terminal: str | None = None
    publications: int = 0


def successors(state: State) -> tuple[State, ...]:
    """All bounded owner, crash, timeout and duplicate-delivery transitions."""
    result = [state]  # A concurrent duplicate waits without changing ownership.
    if state.terminal is not None:
        if state.publications < 2:
            result.append(
                replace(
                    state,
                    stage=Stage.PUBLISHED,
                    publications=state.publications + 1,
                )
            )
        return tuple(result)
    if not state.alive:
        if state.stage in {Stage.REQUESTED, Stage.CLAIMED, Stage.LOADED}:
            if state.fence < 2:
                result.append(
                    replace(
                        state, stage=Stage.CLAIMED, alive=True, fence=state.fence + 1
                    )
                )
        else:
            result.append(
                replace(state, stage=Stage.STORED, alive=True, terminal="timeout")
            )
        return tuple(result)
    result.append(replace(state, alive=False))
    result.append(replace(state, stage=Stage.STORED, terminal="timeout"))
    next_stage = {
        Stage.REQUESTED: Stage.CLAIMED,
        Stage.CLAIMED: Stage.LOADED,
        Stage.LOADED: Stage.INTENT,
        Stage.INTENT: Stage.STARTED,
        Stage.STARTED: Stage.REPORT,
        Stage.REPORT: Stage.STORED,
    }[state.stage]
    result.append(
        replace(
            state,
            stage=next_stage,
            fence=state.fence + int(state.stage == Stage.REQUESTED),
            starts=state.starts + int(next_stage == Stage.STARTED),
            terminal="completed" if next_stage == Stage.STORED else None,
        )
    )
    return tuple(result)
