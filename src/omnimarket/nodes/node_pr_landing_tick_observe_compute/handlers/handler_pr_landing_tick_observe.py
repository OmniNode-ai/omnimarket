# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Observe mode: replay one live tick's facts through the node and compare the decisions."""

from __future__ import annotations

import json
from typing import Any

from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing
from omnimarket.nodes.node_pr_landing_tick_compute import decide_landing_fair_share
from omnimarket.nodes.node_pr_landing_tick_observe_compute.models.model_landing_tick_observation import (
    ModelLandingTickComparison,
    ModelLandingTickDifference,
    ModelLandingTickObservation,
)

ABSENT = "<absent>"


def _text(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def diff_paths(
    controller: object, node: object, path: str = "$"
) -> list[ModelLandingTickDifference]:
    """Every path where two JSON documents differ, in document order."""
    if isinstance(controller, dict) and isinstance(node, dict):
        out: list[ModelLandingTickDifference] = []
        for key in sorted({*controller, *node}):
            here = f"{path}.{key}"
            if key not in controller:
                out.append(
                    ModelLandingTickDifference(
                        path=here, controller=ABSENT, node=_text(node[key])
                    )
                )
            elif key not in node:
                out.append(
                    ModelLandingTickDifference(
                        path=here, controller=_text(controller[key]), node=ABSENT
                    )
                )
            else:
                out += diff_paths(controller[key], node[key], here)
        return out
    if isinstance(controller, list) and isinstance(node, list):
        out = []
        for i in range(max(len(controller), len(node))):
            here = f"{path}[{i}]"
            if i >= len(controller):
                out.append(
                    ModelLandingTickDifference(
                        path=here, controller=ABSENT, node=_text(node[i])
                    )
                )
            elif i >= len(node):
                out.append(
                    ModelLandingTickDifference(
                        path=here, controller=_text(controller[i]), node=ABSENT
                    )
                )
            else:
                out += diff_paths(controller[i], node[i], here)
        return out
    if controller == node:
        return []
    return [
        ModelLandingTickDifference(
            path=path, controller=_text(controller), node=_text(node)
        )
    ]


class HandlerPrLandingTickObserve:
    """Observe mode of the landing tick: pure definition-B compute, no effect and no clock."""

    def handle(
        self, request: ModelLandingTickObservation
    ) -> ModelLandingTickComparison:
        node = (
            decide_landing_fair_share(request.facts)
            if request.fair_share
            else decide_landing(request.facts)
        )
        node_doc: dict[str, Any] = node.model_dump(mode="json")
        controller_doc: dict[str, Any] = request.controller_decision.model_dump(
            mode="json"
        )
        differences = tuple(diff_paths(controller_doc, node_doc))
        return ModelLandingTickComparison(
            tick=request.facts.tick,
            equal=not differences,
            controller_actions=len(request.controller_decision.actions),
            node_actions=len(node.actions),
            differences=differences,
        )


__all__: list[str] = ["ABSENT", "HandlerPrLandingTickObserve", "diff_paths"]
