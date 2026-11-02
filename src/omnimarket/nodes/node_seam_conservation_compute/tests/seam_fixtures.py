# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures shared by the seam conservation tests: the declared graph and a count builder."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.seam_reading import (
    EnumSeamDisposition,
    EnumSeamMeasureKind,
    EnumSeamSide,
    EnumSeamUnit,
    ModelSeamCount,
    ModelSeamIdDisposition,
    ModelSeamMeasure,
    ModelSeamWindow,
    ModelSeamWindowClose,
)

NODES = Path(__file__).resolve().parents[2]
ORCHESTRATOR_CONTRACT = NODES / "node_seam_read_orchestrator" / "contract.yaml"
GRAPH_NAME = "view_seam_read"
COUNT_TOPIC = "onex.evt.omnimarket.view-seam-counted.v1"
CLOSE_TOPIC = "onex.evt.omnimarket.view-seam-window-closed.v1"
READING_TOPIC = "onex.evt.omnimarket.view-seam-reading-recorded.v1"
T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
WINDOW = ModelSeamWindow(start=T0, end=T0 + timedelta(minutes=10))
TICK = "tick-1"


def published_topics(contract: dict[str, Any]) -> set[str]:
    """Topics a contract declares it produces: event_bus.publish_topics and published_events[].topic."""
    topics = set((contract.get("event_bus") or {}).get("publish_topics") or [])
    for event in contract.get("published_events") or []:
        if isinstance(event, dict) and "topic" in event:
            topics.add(event["topic"])
    return topics


def load_graph(path: Path = ORCHESTRATOR_CONTRACT) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    graph = raw["workflow_coordination"]["additional_workflows"][GRAPH_NAME]
    assert isinstance(graph, dict)
    return graph


def seam_nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        n
        for n in graph["execution_graph"]["nodes"]
        if n["node_type"] == "COMPUTE_GENERIC"
    ]


def seam_node(seam_id: str) -> dict[str, Any]:
    for node in seam_nodes(load_graph()):
        if node["step_config"]["seam_id"] == seam_id:
            return node
    raise AssertionError(seam_id)


def stamp(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def ident(
    ident_id: str,
    disposition: EnumSeamDisposition,
    *,
    cause: str | None = None,
    at: int | None = 1,
) -> ModelSeamIdDisposition:
    return ModelSeamIdDisposition.model_validate(
        {
            "id": ident_id,
            "disposition": disposition,
            "cause": cause,
            "stamp": stamp(at) if at is not None else None,
        }
    )


def count(
    seam_id: str,
    side: EnumSeamSide,
    crossing_id: str,
    measures: dict[str, tuple[EnumSeamUnit, list[ModelSeamIdDisposition]]],
    *,
    kind: EnumSeamMeasureKind = EnumSeamMeasureKind.TRANSIT,
    artifacts: dict[str, str] | None = None,
) -> ModelSeamCount:
    return ModelSeamCount.model_validate(
        {
            "seam_id": seam_id,
            "side": side,
            "crossing_id": crossing_id,
            "origin_tick_id": TICK,
            "measures": [
                ModelSeamMeasure(measure_id=name, unit=unit, kind=kind, ids=tuple(ids))
                for name, (unit, ids) in measures.items()
            ],
            "artifacts": [
                {"kind": kind_name, "digest": digest}
                for kind_name, digest in (artifacts or {}).items()
            ],
        }
    )


def close(
    seam_id: str, side: EnumSeamSide, crossing_ids: tuple[str, ...]
) -> ModelSeamWindowClose:
    return ModelSeamWindowClose(
        seam_id=seam_id,
        side=side,
        window=WINDOW,
        reported_through=WINDOW.end,
        crossing_ids=crossing_ids,
    )
