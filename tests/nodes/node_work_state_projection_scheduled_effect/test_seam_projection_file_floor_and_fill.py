# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the projection the scheduled node publishes, the floor's reader and the lab fill's owner reader (OMN-23427).

One scheduled run in the ``file_sources`` profile writes the projection file where it always was and publishes the same
projection as the ``work_state`` floor projection. The floor takes its lanes from the bus through its own reader (the
bundle it builds over the topic's publications), the fill takes its owners from that bundle's ``work_state`` entry, and
the file-path reader still reads the written file. The tests fail when the topic the producer publishes on is not the
one the floor's reader names, when a lane whose CLAIM names a ticket stops showing as that ticket's lane on the floor
and as that ticket's owner in the fill, and when a publication the readers cannot validate reads as unknown, never as
an empty answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.floor_projection import (
    InMemorySnapshotSource,
    envelope_publisher,
    projection_payload,
)
from omnimarket.models.floor_projection import (
    ModelFloorProjectionBundle,
    ModelFloorProjectionEnvelope,
    ModelFloorProjectionReadOverlay,
)
from omnimarket.models.work_state_projection import ModelWorkStateResult
from omnimarket.nodes.node_floor_projection_read_effect.handlers.handler_floor_projection_read import (
    HandlerFloorProjectionRead,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    work_state_from_payload,
    work_state_lanes,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import local_lab_fill_adapters
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalOwnerReader,
)
from omnimarket.nodes.node_work_state_projection_scheduled_effect.handlers.handler_work_state_projection_scheduled import (
    HandlerWorkStateProjectionScheduled,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import (
    RULES_EXAMPLE,
    SRC,
    schedule,
    tick,
)
from tests.unit.nodes.node_overlay_store.support import FakeBus

pytestmark = pytest.mark.unit

FILL_MAX_AGE_S = 1800.0
TOPIC = "onex.snapshot.projection.floor-work-state.v1"
# The floor's own reader overlay: the work_state projection's topic and the bound it is judged against.
READ_OVERLAY = ModelFloorProjectionReadOverlay.model_validate(
    yaml.safe_load(
        (SRC / "node_floor_projection_read_effect" / "overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
)


def _contract(node: str) -> dict:
    return yaml.safe_load((SRC / node / "contract.yaml").read_text(encoding="utf-8"))


def _home(tmp_path: Path, *, claim_cells: str) -> tuple[Path, dict[str, str], datetime]:
    now = datetime.now(UTC).replace(microsecond=0)
    ledger = tmp_path / "ledger" / "rows.md"
    ledger.parent.mkdir(parents=True)
    stamp = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ledger.write_text(
        f"{stamp} | CLAIM | lane=seam-lane | {claim_cells} | scope text\n",
        encoding="utf-8",
    )
    linear = tmp_path / ".onex_state" / "linear-snapshot"
    linear.mkdir(parents=True)
    (linear / "linear-1.json").write_text(
        json.dumps(
            {
                "operator": "op",
                "issues": [
                    {
                        "id": "ZEP-7",
                        "title": "t",
                        "statusType": "started",
                        "project": "Zephyr",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    watcher = tmp_path / ".claude_scratch" / "pr-watcher"
    watcher.mkdir(parents=True)
    (watcher / "state.json").write_text(json.dumps({"prs": {}}), encoding="utf-8")
    env = {
        "OMNI_HOME": str(tmp_path),
        "ONEX_WORK_STATE_OVERLAY": str(RULES_EXAMPLE),
        _contract("node_work_state_projection_effect")["runtime"]["ledger"][
            "path_env"
        ]: str(ledger),
    }
    return tmp_path, env, now


def _publish(
    env: dict[str, str], now: datetime, monkeypatch: pytest.MonkeyPatch
) -> FakeBus:
    """One scheduled run in the file_sources profile: the publish overlay on, the bus a FakeBus. Returns the bus."""
    overlay = Path(env["OMNI_HOME"]) / "publish-overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {"bus": "kafka", "bus_lane": "test-bus", "projection_id": "work_state"}
        ),
        encoding="utf-8",
    )
    env["ONEX_FLOOR_WORK_STATE_OVERLAY"] = str(overlay)
    bus = FakeBus()

    @contextlib.asynccontextmanager
    async def opened(**_: Any) -> AsyncIterator[FakeBus]:
        yield bus

    monkeypatch.setattr(envelope_publisher, "open_lane_bus", opened)
    handler = HandlerWorkStateProjectionScheduled(
        schedule=schedule(projector="file_sources"), environ=env
    )
    event = handler.handle(tick(now))
    assert event is not None
    return bus


def _floor_bundle(
    topic: str, value: bytes, read_at: datetime
) -> ModelFloorProjectionBundle:
    """The bundle the floor's reader builds from one publication, read the way HandlerFloorProjectionRead reads."""
    return asyncio.run(
        HandlerFloorProjectionRead(
            source=InMemorySnapshotSource({topic: [value]}),
            overlay=READ_OVERLAY,
            clock=lambda: read_at,
        ).handle()
    )


def _bundle_written(
    bundle: ModelFloorProjectionBundle, tmp_path: Path, name: str
) -> str:
    path = tmp_path / name
    path.write_text(bundle.model_dump_json(), encoding="utf-8")
    return str(path)


def test_the_pieces_name_one_topic() -> None:
    """The producer's publish topic, the floor reader's snapshot read and the read overlay's work_state projection."""
    effect = _contract("node_work_state_projection_effect")["runtime"]["publish"][
        "topic"
    ]
    assert effect == TOPIC
    assert effect in _contract("node_floor_projection_read_effect")["snapshot_reads"]
    example = yaml.safe_load(
        (SRC / "node_floor_projection_read_effect" / "overlay.example.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert example["projections"]["work_state"]["topic"] == effect


def test_golden_a_lane_whose_claim_names_a_ticket_reaches_both_readers_after_one_publish(
    tmp_path, monkeypatch
) -> None:
    """One scheduled publish: the floor reads the lane off the bus, the fill reads the ticket's owner off the bundle."""
    _home_dir, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    bus = _publish(env, now, monkeypatch)
    ((topic, key, value),) = bus.published
    assert topic == TOPIC
    assert key == b"work_state"
    bundle = _floor_bundle(topic, value, now)
    payload = projection_payload(bundle, "work_state", now)
    projection = work_state_from_payload(payload.payload)
    assert isinstance(projection, ModelWorkStateResult)
    lanes = {lane.lane: lane for lane in work_state_lanes(projection)}
    assert lanes["seam-lane"].ticket == "ZEP-7"
    assert LocalOwnerReader().work_state_owners_from_bundle(
        _bundle_written(bundle, tmp_path, "projection-reads.json")
    ) == {"ZEP-7": ["seam-lane"]}


def test_golden_the_projection_file_the_scheduled_run_writes_still_serves_the_fill(
    tmp_path, monkeypatch
) -> None:
    """The publication adds a copy; the file the run writes first is still where the file-path reader reads."""
    home, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    bus = _publish(env, now, monkeypatch)
    assert len(bus.published) == 1
    written = _contract("node_work_state_projection_effect")["runtime"]["output"][
        "relpath"
    ]
    projection = ModelWorkStateResult.model_validate_json(
        (home / written).read_text(encoding="utf-8")
    )
    assert isinstance(projection, ModelWorkStateResult)
    monkeypatch.setenv("OMNI_HOME", str(home))
    monkeypatch.delenv("ONEX_WORK_STATE_PATH", raising=False)
    assert LocalOwnerReader().work_state_owners("", FILL_MAX_AGE_S) == {
        "ZEP-7": ["seam-lane"]
    }


def test_error_the_same_ledger_without_the_ticket_cell_leaves_the_ticket_unowned(
    tmp_path, monkeypatch
) -> None:
    """The failing control of the golden above: take the cell away and neither reader holds an owner."""
    _home_dir, env, now = _home(tmp_path, claim_cells="note=none")
    bus = _publish(env, now, monkeypatch)
    ((topic, _, value),) = bus.published
    bundle = _floor_bundle(topic, value, now)
    payload = projection_payload(bundle, "work_state", now)
    assert work_state_from_payload(payload.payload) is not None
    assert (
        LocalOwnerReader().work_state_owners_from_bundle(
            _bundle_written(bundle, tmp_path, "projection-reads.json")
        )
        == {}
    )


def test_error_a_payload_without_counts_reads_unknown_and_the_fill_refuses_it(
    tmp_path, monkeypatch
) -> None:
    """A publication that is not a projection is an unknown to the floor and a refusal in the fill, never an empty answer."""
    _home_dir, env, now = _home(tmp_path, claim_cells="ticket=ZEP-7")
    bus = _publish(env, now, monkeypatch)
    ((topic, _, value),) = bus.published
    envelope = ModelFloorProjectionEnvelope.model_validate_json(value)
    broken = envelope.model_copy(
        update={"payload": {k: v for k, v in envelope.payload.items() if k != "counts"}}
    )
    bundle = _floor_bundle(topic, broken.model_dump_json().encode(), now)
    payload = projection_payload(bundle, "work_state", now)
    assert work_state_from_payload(payload.payload) is None
    with pytest.raises(local_lab_fill_adapters.LabFillPortError, match="not readable"):
        LocalOwnerReader().work_state_owners_from_bundle(
            _bundle_written(bundle, tmp_path, "broken-bundle.json")
        )
