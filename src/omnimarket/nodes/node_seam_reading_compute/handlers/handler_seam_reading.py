# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render seam readings as SEAM, MISSING and UNKNOWN lines (OMN-21459).

Canonical shape: ``handle(ModelSeamReadingRequest) -> ModelSeamReadingResult``. Pure: it
takes readings and ``now`` and reads no clock, network or protocol. The seam nodes, their
``expected_crossings`` and the thresholds come from the factory line contract
(``node_seam_read_orchestrator``), read through ``contract_section``; an absent key
refuses the render naming it, never a code default.

A reading is MISSING when any crossing mismatched, whatever else is true. Otherwise it is
UNKNOWN when it cannot be trusted or proves nothing: an unreadable crossing, an empty window
or a window under the minimum crossings. Otherwise it is clean (``status=OK``).
"""

from __future__ import annotations

import re
from pathlib import Path

from omnimarket.models.seam_reading import ModelSeamReading
from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_seam_reading_compute.models.model_seam_reading_request import (
    ModelSeamNode,
    ModelSeamReadConfig,
    ModelSeamReadingRequest,
    ModelSeamReadingResult,
)

ORCHESTRATOR_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "node_seam_read_orchestrator"
    / "contract.yaml"
)
_SEAM_NODE_ID = re.compile(r"^sm_(\d+)_")
_GRAPH_KEYS = (
    "workflow_coordination",
    "workflow_definition",
    "execution_graph",
    "nodes",
)


def load_seam_read_config(
    path: Path = ORCHESTRATOR_CONTRACT_PATH,
) -> ModelSeamReadConfig:
    """Read ``config.seam_read`` of the factory line contract; fail closed when absent."""
    return ModelSeamReadConfig.model_validate(
        contract_section(path, "config", "seam_read")
    )


def load_seam_nodes(
    path: Path = ORCHESTRATOR_CONTRACT_PATH,
) -> tuple[ModelSeamNode, ...]:
    """Read the seam nodes of the graph in line order from the raw contract mapping.

    The typed graph model drops ``step_config``, so the raw mapping is read. A seam node
    without ``expected_crossings`` refuses naming the key.
    """
    nodes = contract_section(path, *_GRAPH_KEYS)
    if not isinstance(nodes, list):
        raise ValueError(f"{path}: {'.'.join(_GRAPH_KEYS)} must be a list")
    seams: list[ModelSeamNode] = []
    for node in nodes:
        match = _SEAM_NODE_ID.match(str(node.get("node_id", "")))
        if match is None:
            continue
        seam_id = f"SM-{match.group(1)}"
        step = node.get("step_config") or {}
        if "expected_crossings" not in step:
            raise ValueError(
                f"{path}: seam {seam_id} ({node['node_id']}) is missing "
                "step_config.expected_crossings"
            )
        seams.append(
            ModelSeamNode(
                seam_id=seam_id,
                node_id=node["node_id"],
                expected_crossings=step["expected_crossings"],
                bound_minutes=step.get("bound_minutes"),
                shape=step.get("shape"),
                gap=step.get("gap"),
            )
        )
    return tuple(seams)


def _seam_line(reading: ModelSeamReading, status: str, window_hours: int) -> str:
    return (
        f"SEAM {reading.seam_id} crossings={reading.crossings} "
        f"mismatched={reading.mismatched} unreadable={reading.unreadable} "
        f"window={window_hours}h status={status}"
    )


def _unknown_reason(reading: ModelSeamReading, minimum: int) -> str | None:
    if reading.unreadable > 0:
        return f"unreadable={reading.unreadable}"
    if reading.crossings == 0:
        return "empty-window"
    if reading.crossings < minimum:
        return f"below-minimum crossings={reading.crossings} minimum={minimum}"
    return None


class HandlerSeamReading:
    """Render the SEAM lines of a set of seam readings."""

    def __init__(self, contract_path: Path = ORCHESTRATOR_CONTRACT_PATH) -> None:
        self._contract_path = contract_path

    def handle(self, request: ModelSeamReadingRequest) -> ModelSeamReadingResult:
        config = load_seam_read_config(self._contract_path)
        seams = load_seam_nodes(self._contract_path)
        by_seam = {reading.seam_id: reading for reading in request.readings}
        lines: list[str] = []
        for seam in seams:
            reading = by_seam.pop(seam.seam_id, None)
            if reading is None:
                lines.append(f"UNKNOWN seam:{seam.seam_id} reason=reading-absent")
                continue
            lines.extend(self._render(reading, config))
        for seam_id in sorted(by_seam):
            lines.append(f"UNKNOWN seam:{seam_id} reason=not-in-graph")
        return ModelSeamReadingResult(
            lines=tuple(lines), config=config, rendered_at=request.now
        )

    def _render(
        self, reading: ModelSeamReading, config: ModelSeamReadConfig
    ) -> list[str]:
        if reading.mismatched > 0:
            examples = ",".join(reading.examples)
            return [
                _seam_line(reading, "MISSING", config.window_hours),
                f"MISSING seam:{reading.seam_id} mismatched={reading.mismatched}"
                f" unreadable={reading.unreadable} examples={examples}",
            ]
        reason = _unknown_reason(reading, config.default_minimum_crossings)
        if reason is not None:
            return [
                _seam_line(reading, "UNKNOWN", config.window_hours),
                f"UNKNOWN seam:{reading.seam_id} reason={reason}",
            ]
        return [_seam_line(reading, "OK", config.window_hours)]
