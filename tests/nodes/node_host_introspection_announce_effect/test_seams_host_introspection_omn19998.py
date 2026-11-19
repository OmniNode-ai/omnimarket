# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests of the host announcement (OMN-19998): each fails when either side of the seam changes shape.

S1  announcement ``capacity``  <->  the capacity advertisement placement, the lab fill and the floor read today
S2  announced topic payload    <->  the registry's reader (the registry plan owns that side; it parses this model)
S3  contract ``announce`` block <-> published events and the topics the registry reads
S4  gather                     <->  the lab host table row model it reads (name, hostnames, roles, availability, clones, port)
S5  overlay cadence and tools  <->  the capacity probe request's bounds

S1 and S2 run on the bus named by ``HOST_INTROSPECTION_SEAM_BUS`` (default ``inmemory``); with ``kafka`` and
``HOST_INTROSPECTION_SEAM_LANE`` naming a declared lane (and ``HOST_INTROSPECTION_SEAM_WORKSPACE`` the checkout that holds the lane declarations) they run the same assertions on the real broker, publishing for a host
named for the run so no real host is touched.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import BusKind
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_introspection_announcement import (
    ModelHostIntrospectionAnnouncement,
)
from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostRow
from omnimarket.nodes.node_host_introspection_announce_effect.handlers import (
    HandlerHostIntrospectionGather,
    HostAnnouncer,
    load_announce_topics,
)
from omnimarket.nodes.node_lab_work_unit_effect.handlers.handler_host_capacity_advertise_effect import (
    HandlerHostCapacityAdvertiseEffect,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelHostCapacityProbeRequest,
)
from tests.host_introspection_support import (
    NOW,
    FakeCapacityReader,
    FakeFactsReader,
    facts,
    install_overlay,
    table_for,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_host_introspection_announce_effect"
)
SEAM_BUS = os.environ.get("HOST_INTROSPECTION_SEAM_BUS", "inmemory")
SEAM_LANE = os.environ.get("HOST_INTROSPECTION_SEAM_LANE") or None
_WORKSPACE = os.environ.get("HOST_INTROSPECTION_SEAM_WORKSPACE")
SEAM_WORKSPACE = Path(_WORKSPACE) if _WORKSPACE else None
SEAM_WAIT_SECONDS = float(os.environ.get("HOST_INTROSPECTION_SEAM_WAIT", "20"))


async def _announce_on_bus(
    overlay: Any, host: str
) -> tuple[dict[str, Any], ModelHostCapacityAdvertisement]:
    """Publish one announcement for ``host`` on the seam bus; answer the payload that arrived and the old producer's reading."""
    reader = FakeCapacityReader(tools=("git", "claude-login"))
    gather = HandlerHostIntrospectionGather(
        overlay=overlay,
        reader=FakeFactsReader(facts()),
        table=lambda: table_for(host),
        capacity=HandlerHostCapacityAdvertiseEffect(reader, now=lambda: NOW),
        now=lambda: NOW,
    )
    # The old producer, fed the same machine, clock and request the gather builds.
    old = HandlerHostCapacityAdvertiseEffect(reader, now=lambda: NOW).handle(
        ModelHostCapacityProbeRequest(
            host_name=host,
            cadence_seconds=overlay.cadence_seconds,
            tools=list(overlay.capacity_tools),
            running_units=0,
            max_units=overlay.max_units,
        )
    )
    topics = load_announce_topics()
    arrived: list[dict[str, Any]] = []
    async with open_lane_bus(
        bus=cast(BusKind, SEAM_BUS),
        lane=SEAM_LANE,
        kafka_bootstrap=None,
        root=SEAM_WORKSPACE,
    ) as bus:

        async def on_message(message: Any) -> None:
            payload = json.loads(message.value)["payload"]
            if payload.get("host_name") == host:
                arrived.append(payload)

        await bus.subscribe(
            topics.announced_topic,
            on_message=on_message,
            group_id=f"host-introspection-seam-{uuid.uuid4().hex[:8]}",
        )
        await HostAnnouncer(bus, gather, host, overlay.cadence_seconds).announce_once()
        for _ in range(int(SEAM_WAIT_SECONDS / 0.2)):
            if arrived:
                break
            await asyncio.sleep(0.2)
    assert arrived, (
        f"no announcement for {host} reached {SEAM_BUS} within {SEAM_WAIT_SECONDS}s"
    )
    return arrived[0], old


async def test_s1_the_nested_capacity_is_the_advertisement_the_old_producer_makes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    host = f"seam-{uuid.uuid4().hex[:8]}.test"
    payload, old = await _announce_on_bus(overlay, host)
    # field for field: every field of the advertisement is in the nested capacity, and nothing else is
    assert set(payload["capacity"]) == set(ModelHostCapacityAdvertisement.model_fields)
    nested = ModelHostCapacityAdvertisement.model_validate(payload["capacity"])
    assert nested == old
    # the field the announcement's ``capacity`` is typed as is the advertisement model itself
    assert (
        ModelHostIntrospectionAnnouncement.model_fields["capacity"].annotation
        is ModelHostCapacityAdvertisement
    )


async def test_s2_the_payload_on_the_topic_is_the_announcement_a_registry_reader_parses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    host = f"seam-{uuid.uuid4().hex[:8]}.test"
    payload, _ = await _announce_on_bus(overlay, host)
    assert set(payload) == set(ModelHostIntrospectionAnnouncement.model_fields)
    parsed = ModelHostIntrospectionAnnouncement.model_validate(payload)
    assert parsed.host_name == host
    assert (parsed.schema_version, parsed.announcement_kind) == (1, "host")
    assert parsed.facts_digest == parsed.expected_facts_digest()


async def test_control_s1_a_producer_that_adds_a_capacity_field_breaks_the_advertisement_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The injected break: the capacity gains a field the advertisement does not have. The old readers refuse it."""
    overlay = install_overlay(monkeypatch, tmp_path)
    payload, _ = await _announce_on_bus(overlay, f"seam-{uuid.uuid4().hex[:8]}.test")
    drifted = {**payload["capacity"], "gpu_count": 2}
    with pytest.raises(ValidationError):
        ModelHostCapacityAdvertisement.model_validate(drifted)
    with pytest.raises(ValidationError):
        ModelHostIntrospectionAnnouncement.model_validate(
            {**payload, "capacity": drifted}
        )
    without = {k: v for k, v in payload["capacity"].items() if k != "cores"}
    with pytest.raises(ValidationError):
        ModelHostIntrospectionAnnouncement.model_validate(
            {**payload, "capacity": without}
        )


async def test_control_s2_a_payload_with_a_changed_fact_and_the_old_digest_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    payload, _ = await _announce_on_bus(overlay, f"seam-{uuid.uuid4().hex[:8]}.test")
    tampered = {**payload, "runtimes": []}
    with pytest.raises(ValidationError, match="facts_digest"):
        ModelHostIntrospectionAnnouncement.model_validate(tampered)


def test_s3_the_contract_topics_are_the_published_and_consumed_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    announce = contract["announce"]
    published = {e["topic"] for e in contract["published_events"]}
    assert published == {announce["announced_topic"], announce["failed_topic"]}
    assert set(contract["externally_consumed_topics"]) == published
    for topic in published:
        assert topic.startswith("onex.evt.omnimarket.")
        assert topic.endswith(".v1")
    types = {e["topic"]: e["event_type"] for e in contract["published_events"]}
    announced_type = types[announce["announced_topic"]]
    assert announced_type == ModelHostIntrospectionAnnouncement.__name__


def test_s4_the_gather_reads_only_columns_the_lab_host_row_has() -> None:
    used = {
        "name",
        "hostnames",
        "roles",
        "available",
        "unavailable_reason",
        "model_port",
        "clones",
        "penalty",
    }
    assert used <= set(ModelLabHostRow.model_fields)


def test_s5_the_overlay_example_fits_the_capacity_probe_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = install_overlay(monkeypatch, tmp_path)
    ModelHostCapacityProbeRequest(
        host_name="host-a.test",
        cadence_seconds=overlay.cadence_seconds,
        tools=list(overlay.capacity_tools),
        max_units=overlay.max_units,
    )
    with pytest.raises(ValidationError):
        ModelHostCapacityProbeRequest(
            host_name="host-a.test", cadence_seconds=overlay.cadence_seconds + 600
        )
