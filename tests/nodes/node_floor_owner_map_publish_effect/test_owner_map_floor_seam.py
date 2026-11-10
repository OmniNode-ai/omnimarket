# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the owner map's producer and the floor compute that reads it (OMN-23402).

Three pieces share the owner map: ``node_floor_owner_map_publish_effect`` publishes it as the ``owner_map`` floor projection,
``node_floor_projection_read_effect`` reads it off the snapshot topic into the bundle, and ``node_floor_simple_effect`` judges
the bundle's projection against its bound and names each pull request's owner from it. They agree on the snapshot topic, the
projection id, the bundle's path and the payload's shape, and each lives in its own contract. This file measures those
agreements (part A), runs the real producer, reader and floor read end to end (part B) and carries the same publication over
a bus, in memory and, when ``OWNER_MAP_SEAM_BOOTSTRAP`` names a lab broker, over Redpanda (part C). Every control is a named
break shown red, beside the same check green without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.container.container_inmemory import bind_infra_event_bus
from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.handlers.floor_projection import (
    BusSnapshotSource,
    InMemorySnapshotSource,
    ProjectionPayload,
    load_bundle,
    owners_from_payload,
    parse_controller_state,
    projection_payload,
)
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.models.floor_projection import (
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionPublishOverlay,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    InMemoryEnvelopePublisher,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.runtime_floor_owner_map_publish import (
    BusEnvelopePublisher,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.runtime_floor_owner_map_publish import (
    run as publish_owner_map,
)
from omnimarket.nodes.node_floor_projection_read_effect.runtime_floor_projection_read import (
    run as read_projections,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.worklist_sources import (
    projected_in_scope_prs,
    read_worklist,
)
from tests.nodes.node_floor_simple_effect.test_worklist_floor_simple import (
    CONFLICT,
    SCOPE,
    TICK,
    WATCHED,
    WATCHER,
    declared_tickets,
    projection,
    write_state,
)
from tests.nodes.node_floor_simple_effect.test_worklist_floor_simple import (
    OVERLAY as WORKLIST_OVERLAY,
)

pytestmark = pytest.mark.unit

BOOTSTRAP_ENV = "OWNER_MAP_SEAM_BOOTSTRAP"
LANE_ENV = "OWNER_MAP_SEAM_LANE"
NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
PUBLISH_NODE = "node_floor_owner_map_publish_effect"
READ_NODE = "node_floor_projection_read_effect"
FLOOR_NODE = "node_floor_simple_effect"
PROJECTION_ID = "owner_map"
BOUND_S = 900
T0 = TICK - timedelta(seconds=30)
PROJ = projection(declared_tickets())
RED = sorted(
    k
    for k, v in WATCHED["prs"].items()
    if v["cls"] == "red" and k in projected_in_scope_prs(PROJ)
)
LANDING_PR, CAUSE_PR = RED[0], RED[1]
LEASES = [
    {"pr": f"OmniNode-ai/{LANDING_PR}", "lease_id": 7},
    {"pr": "cause-key-1", "lease_id": 8},
]
CAUSES = [{"key": "cause-key-1", "members": [{"pr": CAUSE_PR}]}]


def _load(node: str, name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _documents() -> dict[str, dict[str, Any]]:
    return {
        "publisher_contract": _load(PUBLISH_NODE, "contract.yaml"),
        "publisher_overlay": _load(PUBLISH_NODE, "overlay.example.yaml"),
        "reader_contract": _load(READ_NODE, "contract.yaml"),
        "reader_overlay": _load(READ_NODE, "overlay.example.yaml"),
        "floor_contract": _load(FLOOR_NODE, "contract.yaml"),
    }


def seam_findings(
    publisher_contract: dict[str, Any],
    publisher_overlay: dict[str, Any],
    reader_contract: dict[str, Any],
    reader_overlay: dict[str, Any],
    floor_contract: dict[str, Any],
) -> list[str]:
    """One finding per disagreement between the three sides' own declarations; empty when they agree."""
    found: list[str] = []
    topic = publisher_contract["runtime"]["topic"]
    declared = (
        publisher_contract["event_bus"]["publish_topics"][0],
        publisher_contract["externally_consumed_topics"][0],
    )
    if any(t != topic for t in declared):
        found.append(
            f"topic: the publisher declares {topic} and {declared} as its topic"
        )
    listens = reader_overlay["projections"].get(PROJECTION_ID, {}).get("topic")
    if listens != topic:
        found.append(
            f"topic: the reader listens on {listens}, the publisher on {topic}"
        )
    if topic not in reader_contract["snapshot_reads"]:
        found.append(
            f"topic: the reader contract does not list {topic} as a snapshot read"
        )
    sources = floor_contract["runtime"]["sources"]
    publishes_as = publisher_overlay["projection_id"]
    if publishes_as not in reader_overlay["projections"]:
        found.append(f"projection id: the reader has no projection {publishes_as}")
    if publishes_as != sources["owner_map_projection"]:
        found.append(
            f"projection id: the publisher publishes {publishes_as}, the floor reads {sources['owner_map_projection']}"
        )
    if (
        reader_contract["runtime"]["output_relpath"]
        != sources["projection_reads_relpath"]
    ):
        found.append(
            "bundle path: the reader writes "
            f"{reader_contract['runtime']['output_relpath']}, the floor reads {sources['projection_reads_relpath']}"
        )
    bound = reader_overlay["projections"].get(PROJECTION_ID, {}).get("max_age_s")
    if not isinstance(bound, int) or bound <= 0:
        found.append(f"bound: the owner map has no positive bound ({bound!r})")
    return found


# -------------------------------------------------------------------------------------------------- part A: declarations


def test_golden_the_three_sides_agree_on_topic_id_and_bundle_path() -> None:
    assert seam_findings(**_documents()) == []


def test_named_break_the_reader_listens_on_another_topic() -> None:
    docs = _documents()
    assert seam_findings(**docs) == []
    docs = copy.deepcopy(docs)
    docs["reader_overlay"]["projections"][PROJECTION_ID]["topic"] += ".v2"
    findings = seam_findings(**docs)
    assert any(f.startswith("topic: the reader listens on") for f in findings)


def test_named_break_the_publisher_renames_the_projection() -> None:
    docs = _documents()
    assert seam_findings(**docs) == []
    docs = copy.deepcopy(docs)
    docs["publisher_overlay"]["projection_id"] = "owners"
    findings = seam_findings(**docs)
    assert any("the publisher publishes owners" in f for f in findings)


def test_named_break_the_floor_reads_the_bundle_from_another_path() -> None:
    docs = _documents()
    assert seam_findings(**docs) == []
    docs = copy.deepcopy(docs)
    docs["floor_contract"]["runtime"]["sources"]["projection_reads_relpath"] += ".old"
    assert any(f.startswith("bundle path:") for f in seam_findings(**docs))


def test_named_break_the_floor_reads_another_projection_id() -> None:
    docs = _documents()
    assert seam_findings(**docs) == []
    docs = copy.deepcopy(docs)
    docs["floor_contract"]["runtime"]["sources"]["owner_map_projection"] = "owners"
    assert any("the floor reads owners" in f for f in seam_findings(**docs))


def test_named_break_a_projection_with_no_bound() -> None:
    docs = _documents()
    assert seam_findings(**docs) == []
    docs = copy.deepcopy(docs)
    docs["reader_overlay"]["projections"][PROJECTION_ID]["max_age_s"] = 0
    assert any(f.startswith("bound:") for f in seam_findings(**docs))


# ---------------------------------------------------------------------------------- part B: the chain across the seam, in memory


def _topic() -> str:
    topic = _load(PUBLISH_NODE, "contract.yaml")["runtime"]["topic"]
    assert isinstance(topic, str)
    return topic


def _chain(
    tmp_path: Path,
    leases: list[dict[str, Any]] | None,
    *,
    published_as: str = PROJECTION_ID,
    published_at: datetime = T0,
    judged_at: datetime = TICK,
) -> tuple[bool, ProjectionPayload, Any]:
    """Publish the controller's state, read it back through the reader, judge it as the floor does, read the work list."""
    ctl = tmp_path / "ctl"
    ctl.mkdir(parents=True, exist_ok=True)
    if leases is not None:
        write_state(ctl / "state.json", leases, CAUSES)
    publisher = InMemoryEnvelopePublisher()
    result = asyncio.run(
        publish_owner_map(
            ctl,
            ModelFloorProjectionPublishOverlay(
                bus="inmemory", projection_id=published_as
            ),
            publisher,
            InMemoryEmissionSink(),
            producer="ctl",
            clock=lambda: published_at,
        )
    )
    held = InMemorySnapshotSource(
        {_topic(): [e.model_dump_json().encode() for e in publisher.published]}
    )
    overlay = ModelFloorProjectionReadOverlay.model_validate(
        {
            "bus": "inmemory",
            "read_window_s": 0.1,
            "projections": {PROJECTION_ID: {"topic": _topic(), "max_age_s": BOUND_S}},
        }
    )
    read = asyncio.run(
        read_projections(
            tmp_path / "home",
            overlay,
            held,
            InMemoryEmissionSink(),
            clock=lambda: judged_at,
        )
    )
    judged = projection_payload(
        load_bundle(Path(read.output_path)), PROJECTION_ID, judged_at
    )
    items = read_worklist(
        watcher_path=WATCHER,
        owners=owners_from_payload(judged.payload),
        projection=PROJ,
        scope=SCOPE,
        overlay=WORKLIST_OVERLAY,
        excluded_classes=("held-excluded",),
        conflict_states=CONFLICT,
        now=judged_at,
    )
    return result.published, judged, items


def _rows(items: Any) -> dict[str, Any]:
    assert items is not None
    return {i.key: i for i in items if i.kind == "pr"}


def test_golden_chain_the_row_names_the_owner_the_controller_state_held(
    tmp_path: Path,
) -> None:
    published, judged, items = _chain(tmp_path, LEASES)
    rows = _rows(items)
    assert published
    assert judged.as_of == T0
    assert rows[LANDING_PR].landing_lease == "landing-L7"
    assert rows[CAUSE_PR].cause_lease.startswith("cause-L8")


def test_golden_chain_an_owner_change_shows_after_one_publish(tmp_path: Path) -> None:
    changed = [{**LEASES[0], "lease_id": 9}, LEASES[1]]
    _, _, before = _chain(tmp_path / "a", LEASES)
    _, _, after = _chain(tmp_path / "b", changed)
    assert _rows(before)[LANDING_PR].landing_lease == "landing-L7"
    assert _rows(after)[LANDING_PR].landing_lease == "landing-L9"


def test_error_chain_a_state_that_cannot_be_read_leaves_the_work_list_unread_not_empty(
    tmp_path: Path,
) -> None:
    published, judged, items = _chain(tmp_path, None)
    assert published is False
    assert judged.payload is None
    assert items is None
    # The control: the same chain with a readable state yields rows.
    assert _rows(_chain(tmp_path / "ok", LEASES)[2])


def test_error_chain_a_map_older_than_its_bound_leaves_the_work_list_unread(
    tmp_path: Path,
) -> None:
    _, judged, items = _chain(tmp_path, LEASES, judged_at=T0 + timedelta(hours=3))
    assert judged.payload is None
    assert "over its 900s bound" in judged.reason
    assert items is None


def test_named_break_a_publisher_that_renames_its_projection_leaves_the_work_list_unread(
    tmp_path: Path,
) -> None:
    _, judged, items = _chain(tmp_path / "broken", LEASES, published_as="owners")
    assert judged.payload is None
    assert items is None
    _, ok, rows = _chain(tmp_path / "ok", LEASES)
    assert ok.payload is not None
    assert _rows(rows)


# ------------------------------------------------------------------------------------------- part C: the chain over a bus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend",
    [
        pytest.param("inmemory", id="inmemory"),
        pytest.param(
            "redpanda",
            id="redpanda",
            marks=[pytest.mark.integration, pytest.mark.kafka],
        ),
    ],
)
async def test_chain_over_the_bus(backend: str, tmp_path: Path) -> None:
    """The producer's publication, as the contract's publisher sends it, is what the reader's listener receives."""
    topic = f"{_topic()}.seam-{uuid.uuid4().hex[:8]}"
    state = json.dumps({"leases": LEASES, "causes": CAUSES})
    owners = parse_controller_state(state)
    assert owners is not None
    envelope = ModelFloorProjectionEnvelope(
        projection_id=PROJECTION_ID,
        as_of=datetime.now(UTC),
        producer="ctl",
        payload=owners.payload(),
    )

    if backend == "inmemory":
        bus = bind_infra_event_bus(environment="local", group="owner-map-seam")
        await bus.start()

        @contextlib.asynccontextmanager
        async def open_bus() -> AsyncIterator[Any]:
            yield bus

    else:
        bootstrap = os.environ.get(BOOTSTRAP_ENV)
        lane = os.environ.get(LANE_ENV)
        if not (bootstrap or lane):
            pytest.skip(
                f"Kafka not reachable: {BOOTSTRAP_ENV} and {LANE_ENV} name no lab broker"
            )

        def open_bus() -> Any:
            return open_lane_bus(
                bus="kafka",
                lane=None if bootstrap else lane,
                kafka_bootstrap=bootstrap,
                root=workspace_root(os.environ),
            )

    reader = BusSnapshotSource(
        open_bus=open_bus, group_prefix="owner-map-seam", window_s=3.0
    )
    listen = asyncio.create_task(reader.publications(topic))
    await asyncio.sleep(0.5)
    await BusEnvelopePublisher(open_bus, topic).publish(envelope)
    publications = await listen
    assert len(publications) == 1
    received = ModelFloorProjectionEnvelope.model_validate_json(publications[0])
    assert received.projection_id == PROJECTION_ID
    received_owners = owners_from_payload(received.payload)
    assert received_owners is not None
    assert received_owners.of(LANDING_PR) == ("landing-L7", "")
    assert received_owners.of(CAUSE_PR)[1].startswith("cause-L8")
