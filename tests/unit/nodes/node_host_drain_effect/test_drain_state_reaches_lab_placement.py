# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain store's state reaches the lab placement that a caller runs (OMN-22905 review finding).

Before, no caller passed ``draining_hosts``: a drained host kept receiving work units. The placement handler now reads
the drain state on every placement, so every caller gets it with no wiring; an unreadable state refuses the placement
instead of reading as no host draining.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.lab_work.bus import LabWorkCaller
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostsTable
from omnimarket.nodes.node_host_drain_effect import runtime_host_drain as drain_cli
from omnimarket.nodes.node_host_drain_effect.overlay import (
    NODE_NAME,
    ModelHostDrainOverlay,
)
from omnimarket.nodes.node_lab_placement_compute.handlers.handler_lab_pool_place import (
    HandlerLabPoolPlace,
)
from omnimarket.nodes.node_lab_placement_compute.models import (
    EnumPlacementDecision,
    EnumRefusalReason,
)

NOW = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


def _ad(name: str) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=name,
        cores=8,
        load1=0.1,
        mem_available_bytes=32 * 1024**3,
        tools=[],
        running_units=0,
        max_units=4,
        advertised_at=NOW - timedelta(seconds=5),
        cadence_seconds=60,
    )


def _table() -> ModelLabHostsTable:
    return ModelLabHostsTable.model_validate(
        {
            "hosts": [
                {"name": "example-host-a", "target": "a.example", "available": True},
                {"name": "example-host-b", "target": "b.example", "available": True},
            ]
        }
    )


def _caller(
    draining_hosts: Callable[[], tuple[str, ...]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> LabWorkCaller:
    caller = LabWorkCaller(
        EventBusInmemory(environment="local", group="drain-placement-test"),
        now=lambda: NOW,
        placer=HandlerLabPoolPlace(
            table_source=_table, draining_hosts=draining_hosts, environ=environ
        ),
    )
    caller._ads.extend([_ad("example-host-a"), _ad("example-host-b")])
    return caller


def test_a_drained_host_is_not_placed_by_the_caller() -> None:
    caller = _caller(draining_hosts=lambda: ("example-host-a",))
    placement = caller.place(only_host="example-host-a")
    assert placement.decision is EnumPlacementDecision.REFUSED
    assert any("draining" in v.reason for v in placement.verdicts)
    assert caller.place().host_name == "example-host-b"


def test_with_no_drain_state_the_caller_places_as_before() -> None:
    placement = _caller(environ={}).place(only_host="example-host-a")
    assert placement.decision is EnumPlacementDecision.PLACED


def test_an_unreadable_drain_state_stops_the_placement() -> None:
    def unreadable() -> tuple[str, ...]:
        raise OSError("drain store unreadable")

    placement = _caller(draining_hosts=unreadable).place()
    assert placement.decision is EnumPlacementDecision.REFUSED
    assert placement.refusal is EnumRefusalReason.COULD_NOT_CHECK
    assert "drain store unreadable" in placement.detail
    assert placement.placed is False


def test_the_node_reader_returns_the_hosts_the_drain_command_set(
    overlay: ModelHostDrainOverlay, tmp_path: Path
) -> None:
    document = overlay.model_dump(mode="json")
    document["profile"] = "jsonl"
    document["lanes"]["count_command"] = ["python3", "-c", "print(0)", "{host}"]
    loaded = ModelHostDrainOverlay.model_validate(document)
    environ = {loaded.emission.home_env: str(tmp_path)}
    assert (
        drain_cli.run(
            ["drain", "--host", "example-host-a"], overlay=loaded, environ=environ
        )
        == 0
    )
    assert drain_cli.draining_hosts_of(loaded, environ) == ("example-host-a",)


def test_no_drain_overlay_selected_reads_as_no_host_draining() -> None:
    assert drain_cli.read_draining_hosts({}) == ()


def _selected_overlay(
    overlay: ModelHostDrainOverlay, root: Path, home: Path, profile: str
) -> dict[str, str]:
    document = overlay.model_dump(mode="json")
    document["profile"] = profile
    node_dir = root / NODE_NAME
    node_dir.mkdir(parents=True)
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(root),
        overlay.emission.home_env: str(home),
    }


def test_the_default_placer_reads_the_drain_state_the_drain_command_wrote(
    overlay: ModelHostDrainOverlay, tmp_path: Path
) -> None:
    """No caller passes anything: the placement handler reads the node's drain store on its own."""
    environ = _selected_overlay(overlay, tmp_path / "roots", tmp_path / "home", "jsonl")
    document = overlay.model_dump(mode="json")
    document["profile"] = "jsonl"
    document["lanes"]["count_command"] = ["python3", "-c", "print(0)", "{host}"]
    loaded = ModelHostDrainOverlay.model_validate(document)
    assert (
        drain_cli.run(
            ["drain", "--host", "example-host-a"], overlay=loaded, environ=environ
        )
        == 0
    )
    caller = _caller(environ=environ)
    refused = caller.place(only_host="example-host-a")
    assert refused.decision is EnumPlacementDecision.REFUSED
    assert any("draining" in v.reason for v in refused.verdicts)
    assert caller.place().host_name == "example-host-b"


def test_a_selected_overlay_with_a_missing_deployment_fact_stops_the_placement(
    overlay: ModelHostDrainOverlay, tmp_path: Path
) -> None:
    environ = _selected_overlay(overlay, tmp_path / "roots", tmp_path / "home", "jsonl")
    del environ[overlay.emission.home_env]
    placement = _caller(environ=environ).place()
    assert placement.decision is EnumPlacementDecision.REFUSED
    assert placement.refusal is EnumRefusalReason.COULD_NOT_CHECK
    assert "drain state could not be read" in placement.detail
