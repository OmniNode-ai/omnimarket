# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane-intake marking for nodes that open Linear work items (OMN-17942).

Every Linear item a lane or a node opens is written through the operator's one
existing Linear key, so its author field reads as the operator's own (scorecard
S3: 135 of 148 new items 2026-10-12..2026-10-16). The operator ruled the key
stays, so the item itself carries the fact: a label and a first-line stamp.

The vocabulary (label name, stamp prefix, per-day budget) is the omniclaude
ticket-creation policy's ``lane_intake`` object, read from the same installed
plugin the ticket-creation guard runs from. This module spells none of it, so
the guard and the nodes cannot drift apart on what a lane-created item is.

* :func:`declared_lane` -- the lane identity the launcher declared, if any.
* :func:`stamp_description` -- put the stamp on the first line, once.
* :func:`resolve_label_id` -- the Linear label id for a name, creating it once.
* :func:`admit_lane_intake` -- the guard's lane-intake rules (identity, label,
  stamp, daily budget) for a node whose create does not go through the full
  admission gate; records the admission against the day's budget.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

_LANE_ENV_KEYS = ("ONEX_LANE", "ONEX_REMOTE_LANE")
_GUARD_TIMEOUT_S = 60
#: The plugin's ``hooks/lib/<guard>`` -> ``hooks/config/ticket_creation_policy.json``.
_POLICY_RELPATH = Path("config") / "ticket_creation_policy.json"

_LABEL_QUERY = """
query GetLabel($name: String!) {
  issueLabels(filter: { name: { eq: $name } }, first: 1) { nodes { id } }
}
"""

_LABEL_CREATE_MUTATION = """
mutation CreateLabel($name: String!, $teamId: String!) {
  issueLabelCreate(input: { name: $name, teamId: $teamId }) {
    issueLabel { id }
  }
}
"""


class LaneIntakeRefusedError(RuntimeError):
    """The lane-intake rules refused a create, or could not decide."""


class ModelLaneIntakePolicy(BaseModel):
    """The label and stamp prefix a lane- or node-created item carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    stamp_prefix: str


def read_lane_intake_policy(guard_path: Path) -> ModelLaneIntakePolicy:
    """The ``lane_intake`` vocabulary beside the installed guard, or raise."""
    policy_path = guard_path.parent.parent / _POLICY_RELPATH
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
        intake = raw["lane_intake"]
        return ModelLaneIntakePolicy(
            label=str(intake["label"]), stamp_prefix=str(intake["stamp_prefix"])
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LaneIntakeRefusedError(
            f"cannot read lane_intake from {policy_path} ({exc}); refusing to open "
            "an item that cannot be marked as lane-created (OMN-17942)"
        ) from exc


def declared_lane(environ: Mapping[str, str] | None = None) -> str:
    """The lane identity the launcher declared, ``""`` when it declared none."""
    env = os.environ if environ is None else environ
    return next((env[k].strip() for k in _LANE_ENV_KEYS if env.get(k, "").strip()), "")


def stamp_description(
    description: str, policy: ModelLaneIntakePolicy, lane: str
) -> str:
    """The description with the stamp as its first line; unchanged if present."""
    stamp = f"{policy.stamp_prefix} {lane}"
    if description.split("\n", 1)[0].strip() == stamp:
        return description
    return f"{stamp}\n\n{description}" if description else f"{stamp}\n"


def resolve_label_id(
    post: Callable[[str, dict[str, object]], Any], name: str, team_id: str
) -> str:
    """The id of the label ``name``, created on ``team_id`` when absent."""
    found = (
        post(_LABEL_QUERY, {"name": name})
        .get("data", {})
        .get("issueLabels", {})
        .get("nodes", [])
    )
    if found:
        return str(found[0]["id"])
    created = (
        post(_LABEL_CREATE_MUTATION, {"name": name, "teamId": team_id})
        .get("data", {})
        .get("issueLabelCreate", {})
        .get("issueLabel", {})
    )
    label_id = str(created.get("id", ""))
    if not label_id:
        raise RuntimeError(f"Linear returned no id for label {name!r}")
    return label_id


def admit_lane_intake(
    guard_path: Path, tool_input: dict[str, object], lane: str
) -> None:
    """Run the guard's lane-intake rules for a node's create, or raise.

    Exit 0 admits (and records one admission against today's budget), exit 3
    refuses with the reason as JSON, anything else is a guard that could not
    decide -- also a refusal.
    """
    payload = json.dumps(
        {"tool_name": "mcp__linear-server__save_issue", "tool_input": tool_input}
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if lane:
        env["ONEX_LANE"] = lane
    done = subprocess.run(
        [sys.executable, str(guard_path), "--lane-intake"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=_GUARD_TIMEOUT_S,
        env=env,
        check=False,
    )
    if done.returncode == 0:
        return
    if done.returncode == 3:
        try:
            reason = str(json.loads(done.stdout).get("reason", done.stdout))
        except (ValueError, AttributeError):
            reason = done.stdout
        raise LaneIntakeRefusedError(reason)
    raise LaneIntakeRefusedError(
        f"the lane-intake check could not decide (exit {done.returncode}): "
        f"{done.stderr.strip()[-2000:]}"
    )


class LaneIntakeGate:
    """The lane-intake rules for a node whose create bypasses the full guard.

    ``guard_path`` is the installed ticket-creation guard; ``None`` locates the
    enabled onex plugin the way ``node_create_ticket`` does. A seam for tests:
    both methods are what a fake replaces.
    """

    def __init__(self, guard_path: Path | None = None) -> None:
        self._guard_path = guard_path

    def _guard(self) -> Path:
        if self._guard_path is None:
            from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
                locate_ticket_guard,
            )

            self._guard_path = locate_ticket_guard()
        return self._guard_path

    def policy(self) -> ModelLaneIntakePolicy:
        return read_lane_intake_policy(self._guard())

    def admit(self, tool_input: dict[str, object], lane: str) -> None:
        admit_lane_intake(self._guard(), tool_input, lane)


__all__ = [
    "LaneIntakeGate",
    "LaneIntakeRefusedError",
    "ModelLaneIntakePolicy",
    "admit_lane_intake",
    "declared_lane",
    "read_lane_intake_policy",
    "resolve_label_id",
    "stamp_description",
]
