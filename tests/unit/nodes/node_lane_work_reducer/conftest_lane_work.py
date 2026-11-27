# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Helpers for the lane work reducer tests: a driver that fills completion action ids."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from omnibase_core.models.lane_work import ModelLaneHelpTier

from omnimarket.nodes.node_lane_work_reducer.handlers.handler_lane_work_reducer import (
    HandlerLaneWorkReducer,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_fold_input import (
    ModelLaneWorkFoldInput,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reduce_result import (
    ModelLaneWorkReduceResult,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_config import (
    ModelLaneWorkReducerConfig,
)

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lane_work_reducer/contract.yaml"
)
HEAD = "a" * 40
OTHER = "b" * 40
T0 = "2026-11-26T09:00:00Z"

# Completion events and the outstanding action each answers.
ANSWERS = {
    "claim_confirmed": "ledger_query_claims",
    "claim_missing": "ledger_query_claims",
    "proof_passed": "lab_proof",
    "proof_failed": "lab_proof",
    "proof_posted": "post_lab_comment",
    "proof_post_failed": "post_lab_comment",
    "handoff_written": "handoff_request",
    "handoff_failed": "handoff_request",
    "help_cause_found": "help_request",
    "help_retry": "help_request",
    "help_stop": "help_request",
    "help_failed": "help_request",
}

TIERS = tuple(
    ModelLaneHelpTier(
        name=name, backend_id=f"b-{name}", harness="h", model="m", effort="high"
    )
    for name in ("local", "glm-small", "claude-large")
)


def contract_machine() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text())
    assert isinstance(loaded, dict)
    machine = loaded["state_machine"]
    assert isinstance(machine, dict)
    return machine


class Driver:
    """Folds events one by one, filling the action id a completion answers."""

    def __init__(
        self, entry: str | None = None, config: ModelLaneWorkReducerConfig | None = None
    ) -> None:
        self.handler = HandlerLaneWorkReducer()
        self.config = config or ModelLaneWorkReducerConfig(help_tiers=TIERS)
        self.state = self.handler.initial_state(entry, config=self.config)
        self.results: list[ModelLaneWorkReduceResult] = []
        self.counter = 0

    def outstanding(self, name: str) -> str | None:
        for action_id, action_name in self.state.outstanding_actions.items():
            if action_name == name:
                return action_id
        return None

    def send(
        self, kind: str, now: str | None = T0, **fields: Any
    ) -> ModelLaneWorkReduceResult:
        self.counter += 1
        if kind in ANSWERS and "action_id" not in fields:
            found = self.outstanding(ANSWERS[kind])
            if found is not None:
                fields["action_id"] = found
        fields.setdefault("input_id", f"in-{self.counter}")
        result = self.handler.reduce(
            self.state, ModelLaneWorkFoldInput(kind=kind, **fields), now, self.config
        )
        self.state = result.state
        self.results.append(result)
        return result

    def fire(self, deadline: str, now: str | None = T0) -> ModelLaneWorkReduceResult:
        attempt = self.state.armed_deadlines.get(deadline, 0)
        return self.send(
            "deadline_passed", now, deadline=deadline, deadline_attempt=attempt
        )

    @property
    def name(self) -> str:
        return self.state.name
