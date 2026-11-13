# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24281: the publisher-reader seam of the floor's owner_map and work_state projections.

The floor projection reader reads each projection from the topic its producer publishes on. That one topic
string is written in four places that must agree: the producer contract, the producer's publication overlay
(the owner map's overlay.example.yaml; the work-state node has none, its contract's runtime.publish block names
the publication instead), the reader's overlay.example.yaml and the reader's contract snapshot_reads, and the
producer must be a step module of the floor publish cycle, or the reader listens where nothing is published.
This is a contract-level seam check: no bus, only the YAML the files declare.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODES = REPO / "src" / "omnimarket" / "nodes"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


CYCLE_CONTRACT = _load(NODES / "node_factory_floor_publish_effect" / "contract.yaml")
READER_CONTRACT = _load(NODES / "node_floor_projection_read_effect" / "contract.yaml")
READER_OVERLAY = _load(
    NODES / "node_floor_projection_read_effect" / "overlay.example.yaml"
)
OWNER_MAP = "omnimarket.nodes.node_floor_owner_map_publish_effect"
OWNER_MAP_CONTRACT = _load(
    NODES / "node_floor_owner_map_publish_effect" / "contract.yaml"
)
OWNER_MAP_OVERLAY = _load(
    NODES / "node_floor_owner_map_publish_effect" / "overlay.example.yaml"
)
WORK_STATE = "omnimarket.nodes.node_work_state_projection_effect"
WORK_STATE_CONTRACT = _load(
    NODES / "node_work_state_projection_effect" / "contract.yaml"
)

# The projection id, its producer's module, the producer contract, the producer's
# publication overlay (None when the node has none) and the contract path of the topic.
SEAMS: tuple[
    tuple[str, str, dict[str, Any], dict[str, Any] | None, tuple[str, ...]], ...
] = (
    (
        "owner_map",
        OWNER_MAP,
        OWNER_MAP_CONTRACT,
        OWNER_MAP_OVERLAY,
        ("runtime", "topic"),
    ),
    (
        "work_state",
        WORK_STATE,
        WORK_STATE_CONTRACT,
        None,
        ("runtime", "publish", "topic"),
    ),
)


def _topic(producer: dict[str, Any], path: tuple[str, ...]) -> str:
    node: Any = producer
    for key in path:
        node = node[key]
    return str(node)


def _cycle_step_modules(cycle_contract: dict[str, Any]) -> list[str]:
    steps = cycle_contract.get("runtime", {}).get("steps", [])
    return [s["module"] for s in steps if isinstance(s, dict) and "module" in s]


def seam_problems(
    projection_id: str,
    module: str,
    producer: dict[str, Any],
    producer_overlay: dict[str, Any] | None,
    topic_path: tuple[str, ...],
    reader_overlay: dict[str, Any],
    reader_contract: dict[str, Any],
    cycle_contract: dict[str, Any],
) -> list[str]:
    """Every way the producer, its publication and the reader disagree about one projection."""
    problems: list[str] = []
    topic = _topic(producer, topic_path)
    read = reader_overlay.get("projections", {}).get(projection_id, {}).get("topic")
    if read != topic:
        problems.append(
            f"{projection_id}: the reader's overlay reads topic {read!r}, "
            f"the producer publishes {topic!r}"
        )
    if topic not in reader_contract.get("snapshot_reads", []):
        problems.append(
            f"{projection_id}: the reader's contract lists no snapshot read of {topic!r}"
        )
    if module not in _cycle_step_modules(cycle_contract):
        problems.append(
            f"{projection_id}: the cycle contract runs no {module} step to publish it"
        )
    if producer_overlay is None:
        block = producer.get("runtime", {}).get("publish", {})
        if block.get("overlay_id") != producer.get("name"):
            problems.append(
                f"{projection_id}: the producer contract's publish block names "
                f"overlay_id {block.get('overlay_id')!r}, not {producer.get('name')!r}"
            )
    elif producer_overlay.get("projection_id") != projection_id:
        problems.append(
            f"{projection_id}: the producer's overlay names projection_id "
            f"{producer_overlay.get('projection_id')!r}"
        )
    return problems


@pytest.mark.parametrize(
    ("projection_id", "module", "producer", "producer_overlay", "topic_path"),
    SEAMS,
    ids=["owner_map", "work_state"],
)
def test_seam_the_publisher_and_the_reader_agree_about_every_topic(
    projection_id: str,
    module: str,
    producer: dict[str, Any],
    producer_overlay: dict[str, Any] | None,
    topic_path: tuple[str, ...],
) -> None:
    problems = seam_problems(
        projection_id,
        module,
        producer,
        producer_overlay,
        topic_path,
        READER_OVERLAY,
        READER_CONTRACT,
        CYCLE_CONTRACT,
    )
    assert problems == []


@pytest.mark.parametrize(
    ("projection_id", "module", "producer", "producer_overlay", "topic_path"),
    SEAMS,
    ids=["owner_map", "work_state"],
)
def test_seam_break_control_a_changed_topic_reports_a_problem(
    projection_id: str,
    module: str,
    producer: dict[str, Any],
    producer_overlay: dict[str, Any] | None,
    topic_path: tuple[str, ...],
) -> None:
    broken_overlay = copy.deepcopy(READER_OVERLAY)
    broken_overlay["projections"][projection_id]["topic"] = (
        "onex.snapshot.projection.floor-broken.v1"
    )
    problems = seam_problems(
        projection_id,
        module,
        producer,
        producer_overlay,
        topic_path,
        broken_overlay,
        READER_CONTRACT,
        CYCLE_CONTRACT,
    )
    assert problems != []
