# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure state machine reducer that interprets contract-declared transition tables.

delta(state, response, transitions) -> ReducerResult

No I/O, no network, no side effects. The caller owns state persistence.
"""

from __future__ import annotations

import logging
from typing import Literal, cast

from omnimarket.nodes.node_contract_reducer.models.model_reducer_result import (
    ModelReducerResult,
)

logger = logging.getLogger(__name__)

_TransitionMap = dict[str, object]
_ResponseMap = dict[str, _TransitionMap]


class HandlerContractReducer:
    """Stateless pure reducer — interprets a transition table from a contract."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["compute"] = "compute"

    def reduce(
        self,
        state: dict[str, object],
        response: str,
        transitions: list[dict[str, object]],
    ) -> ModelReducerResult:
        """Apply one transition step.

        Args:
            state: Current accumulated state. Must contain ``current_step``.
            response: The user's response token for the current step.
            transitions: Transition table from the calling contract. Each entry:
                ``{"from": str, "responses": {response_token: {"next": str, "set_state": dict}}}``.

        Returns:
            ModelReducerResult with updated_state, next_step, is_terminal.

        Raises:
            KeyError: current_step missing from state.
            ValueError: No transition declared for current_step, or response
                not in the step's response map.
        """
        current_step = str(state["current_step"])

        transition = _find_transition(transitions, current_step)
        if transition is None:
            raise ValueError(f"No transition declared for step {current_step!r}")

        responses = cast(_ResponseMap, transition.get("responses", {}))
        if response not in responses:
            valid = sorted(responses.keys())
            raise ValueError(
                f"Response {response!r} not valid for step {current_step!r}; "
                f"valid responses: {valid}"
            )

        branch: _TransitionMap = responses[response]
        next_step = str(branch["next"])
        set_state: _TransitionMap = branch.get("set_state", {})  # type: ignore[assignment]

        all_from_steps = {str(t.get("from", "")) for t in transitions}
        is_terminal = next_step == "done" or next_step not in all_from_steps

        updated_state: dict[str, object] = {
            **state,
            **set_state,
            "current_step": next_step,
        }

        logger.debug(
            "reduce: %s --[%s]--> %s (terminal=%s)",
            current_step,
            response,
            next_step,
            is_terminal,
        )

        return ModelReducerResult(
            updated_state=updated_state,
            next_step=next_step,
            is_terminal=is_terminal,
        )

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """Thin dict-in / dict-out wrapper for runtime dispatch."""
        result = self.reduce(
            state=cast(dict[str, object], input_data["current_state"]),
            response=str(input_data["user_response"]),
            transitions=cast(
                list[dict[str, object]], input_data["contract_transitions"]
            ),
        )
        return result.model_dump(mode="json")


def _find_transition(
    transitions: list[dict[str, object]], step: str
) -> dict[str, object] | None:
    for t in transitions:
        if str(t.get("from", "")) == step:
            return t
    return None


__all__: list[str] = ["HandlerContractReducer"]
