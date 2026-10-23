# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which deterministic tools each morning phase agent is given (phase_tools.json)."""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files
from typing import cast

from .base import MorningTool
from .catalog import TOOLS

PACKAGE = "omnimarket.nodes.node_morning_ground_state_orchestrator.tools"


class MorningPhaseToolsMissingError(RuntimeError):
    """The phase has no deterministic tool set yet, so no agent session is started."""


@cache
def phase_tools_contract() -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads(files(PACKAGE).joinpath("phase_tools.json").read_text()),
    )


def _phase(label: str) -> dict[str, list[str]]:
    phases = cast(dict[str, dict[str, list[str]]], phase_tools_contract()["phases"])
    if label not in phases:
        pending = cast(dict[str, list[str]], phase_tools_contract()["pending"]).get(
            label
        )
        raise MorningPhaseToolsMissingError(
            f"{label}: no deterministic tool set yet (RULING 2026-10-21T22:32:15Z, "
            "OMN-17427): a morning phase agent is given only typed tools and never a "
            "shell, so this phase is refused before any session starts"
            + (f"; tools still to build: {', '.join(pending)}" if pending else "")
        )
    return phases[label]


def phase_tools(label: str) -> list[MorningTool]:
    """The tools this phase's agent is given, in contract order; unknown names refuse."""
    names = _phase(label)["tools"]
    unknown = [name for name in names if name not in TOOLS]
    if unknown:
        raise MorningPhaseToolsMissingError(
            f"{label}: phase_tools.json names unbuilt tools {unknown}"
        )
    return [TOOLS[name] for name in names]


#: The phase that pushes the run's knowledge-base branch and opens its one PR.
PUBLISHING_PHASE = "session-goal"


def has_tool_set(label: str) -> bool:
    return label in cast(dict[str, object], phase_tools_contract()["phases"])


def refuse_stranding_commit(label: str) -> None:
    """A committing phase waits until the phase that pushes its branch has its tools.

    The day's knowledge-base branch is pushed only by the publishing phase. A commit made
    while that phase cannot run is never pushed, and the next morning's worktree setup then
    refuses the unmerged prior branch, so the commit would block every later run.
    """
    if phase_writes(label) and not has_tool_set(PUBLISHING_PHASE):
        raise MorningPhaseToolsMissingError(
            f"{label}: commits to the run's knowledge-base branch, but {PUBLISHING_PHASE} "
            "(which pushes it and opens its PR) has no deterministic tool set yet; refused "
            "so no unpushed commit strands the next run's worktree"
        )


def phase_writes(label: str) -> frozenset[str]:
    return frozenset(_phase(label)["writes"])


def phase_lane(label: str) -> tuple[str | None, str | None]:
    """The phase's own ledger lane and ticket, for its CLAIM and TERMINAL rows."""
    phases = cast(dict[str, dict[str, object]], phase_tools_contract()["phases"])
    entry = phases.get(label, {})
    lane, ticket = entry.get("lane"), entry.get("ticket")
    return (
        lane if isinstance(lane, str) else None,
        ticket if isinstance(ticket, str) else None,
    )


def phase_note(label: str) -> str:
    """The phase's tool guide: which tool does each step of its brief, and what has no tool."""
    phases = cast(dict[str, dict[str, object]], phase_tools_contract()["phases"])
    entry = phases.get(label, {})
    note = entry.get("note")
    gaps = cast(dict[str, list[str]], phase_tools_contract().get("gaps", {})).get(
        label, []
    )
    text = ""
    if isinstance(note, list):
        text += "\n\nTOOL GUIDE FOR THIS PHASE:\n" + "\n".join(
            f"- {line}" for line in note
        )
    if gaps:
        text += (
            "\nNO TOOL YET (report each affected value UNKNOWN naming the missing tool, "
            "never 0 and never GREEN): " + "; ".join(gaps)
        )
    return text


def node_step_tool(step: str) -> MorningTool:
    """A step the node runs itself through one tool, with no agent (ledger-reconcile)."""
    steps = cast(dict[str, str], phase_tools_contract()["node_steps"])
    return TOOLS[steps[step]]
