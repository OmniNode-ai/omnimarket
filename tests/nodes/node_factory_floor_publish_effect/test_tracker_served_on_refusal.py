# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A cycle refused at a required input step still serves the delegation tracker and names the refusal (OMN-23969).

The floor record is the one required input: without it no floor page is published. The delegation tracker is built by an
optional step from its own sources and never needs that record, so a refusal at the floor step must not freeze it on the
serving host (it stayed at its 13:34 reading for 90 minutes while the scenarios ran). The served status gains a ``refused``
block (step, detail, when it started) beside every key it already carried, and a cycle that publishes clears it.
Golden chain: a cycle that publishes carries no ``refused`` block. Error chain: a refused cycle serves the tracker and the
block, serves no floor file, and keeps the time the refusals began.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    PublishRefusedError,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishOverlay,
    ModelFactoryFloorPublishRequest,
)

pytestmark = pytest.mark.unit

FACTORY = "omnimarket.nodes.node_factory_floor_effect"
RECORD = {
    "schema_id": "factory-floor/1",
    "generated_at": "2026-11-12T15:00:00+00:00",
    "lines": [],
    "floor": {"bays": [], "shipped": [], "waiting": [], "stoppages": []},
    "sources": {},
}
TRACKER = {
    "schema_id": "omnimarket.floor_delegation.v1",
    "generated_at": "2026-11-12T15:30:00Z",
}
PREVIOUS_STATUS = {"published_at": "2026-11-12T13:31:49+00:00", "cycle_seconds": 178.1}
NOW_TEXT = "2026-11-12T15:40:00+00:00"


class FloorStepRefusing:
    """ssh runs locally; the floor step answers exit 3 when ``refuse`` is set, every other command exits 0."""

    def __init__(self, *, refuse: bool) -> None:
        self.refuse = refuse
        self.ssh_calls = 0

    def __call__(
        self, argv: Sequence[str], stdin: bytes | None, timeout: int
    ) -> subprocess.CompletedProcess[bytes]:
        if argv[0] == "ssh":
            self.ssh_calls += 1
            return subprocess.run(
                ["sh", "-c", argv[-1]], input=stdin, capture_output=True, check=False
            )
        if self.refuse and FACTORY in argv:
            return subprocess.CompletedProcess(list(argv), 3, b"", b"no plans")
        return subprocess.CompletedProcess(list(argv), 0, b"", b"")


def _overlay(tmp_path: Path) -> ModelFactoryFloorPublishOverlay:
    return ModelFactoryFloorPublishOverlay(
        ssh_target="lab-web",
        remote_dir=str(tmp_path / "site"),
        serve_url="http://example.invalid:8088/",
        bus_lane="lane-x",
        history_root=str(tmp_path / "site"),
        owner_login="owner@example.invalid",
        local_copy_relpath="floor-copy",
    )


def _home(tmp_path: Path, *, previous: dict[str, object] | None) -> Path:
    rt = load_runtime()
    home = tmp_path / "home"
    record = home / rt.source.relpath
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps(RECORD), encoding="utf-8")
    tracker = home / rt.delegation_tracker_data_relpath
    tracker.parent.mkdir(parents=True, exist_ok=True)
    tracker.write_text(json.dumps(TRACKER), encoding="utf-8")
    if previous is not None:
        status = home / "floor-copy" / rt.names.status
        status.parent.mkdir(parents=True)
        status.write_text(json.dumps(previous), encoding="utf-8")
    return home


def _cycle(
    tmp_path: Path, *, refuse: bool, previous: dict[str, object] | None
) -> tuple[FloorStepRefusing, Path]:
    ssh = FloorStepRefusing(refuse=refuse)
    home = _home(tmp_path, previous=previous)
    request = ModelFactoryFloorPublishRequest(
        home=str(home), overlay=_overlay(tmp_path)
    )
    handler = HandlerFactoryFloorPublish(ssh)
    if refuse:
        with pytest.raises(PublishRefusedError, match="required step factory_floor"):
            handler.handle(request, environ={})
    else:
        handler.handle(request, environ={})
    return ssh, tmp_path / "site"


def _served_status(site: Path) -> dict[str, object]:
    return json.loads((site / load_runtime().names.status).read_text())


def test_golden_chain_a_publishing_cycle_serves_its_status_with_no_refused_block(
    tmp_path: Path,
) -> None:
    previous = {
        **PREVIOUS_STATUS,
        "refused": {"step": "factory_floor", "since": NOW_TEXT},
    }
    _, site = _cycle(tmp_path, refuse=False, previous=previous)
    status = _served_status(site)
    assert "refused" not in status
    assert status["published_at"] != PREVIOUS_STATUS["published_at"]
    assert json.loads((site / "delegation-tracker.json").read_text()) == TRACKER
    assert (site / "factory-floor.json").exists()


def test_error_chain_a_refused_cycle_serves_the_tracker_and_names_the_refusal(
    tmp_path: Path,
) -> None:
    _, site = _cycle(tmp_path, refuse=True, previous=PREVIOUS_STATUS)
    assert json.loads((site / "delegation-tracker.json").read_text()) == TRACKER
    status = _served_status(site)
    assert status["published_at"] == PREVIOUS_STATUS["published_at"]
    assert status["cycle_seconds"] == PREVIOUS_STATUS["cycle_seconds"]
    refused = status["refused"]
    assert isinstance(refused, dict)
    assert refused["step"] == "factory_floor"
    assert "exit 3" in str(refused["detail"])
    assert refused["since"] == refused["at"]


def test_error_chain_a_refused_cycle_serves_no_floor_file(tmp_path: Path) -> None:
    _, site = _cycle(tmp_path, refuse=True, previous=PREVIOUS_STATUS)
    names = {p.name for p in site.iterdir()}
    assert names == {"delegation-tracker.json", load_runtime().names.status}


def test_error_chain_a_second_refusal_keeps_the_time_the_refusals_began(
    tmp_path: Path,
) -> None:
    earlier = "2026-11-12T14:00:00+00:00"
    previous = {
        **PREVIOUS_STATUS,
        "refused": {"step": "factory_floor", "since": earlier, "at": earlier},
    }
    _, site = _cycle(tmp_path, refuse=True, previous=previous)
    refused = _served_status(site)["refused"]
    assert isinstance(refused, dict)
    assert refused["since"] == earlier
    assert refused["at"] != earlier


def test_control_a_refusal_with_no_previous_status_and_no_tracker_serves_nothing(
    tmp_path: Path,
) -> None:
    """The failing control for the old rule: with nothing to serve the refusal still publishes nothing (no ssh at all)."""
    ssh = FloorStepRefusing(refuse=True)
    home = _home(tmp_path, previous=None)
    (home / load_runtime().delegation_tracker_data_relpath).unlink()
    request = ModelFactoryFloorPublishRequest(
        home=str(home), overlay=_overlay(tmp_path)
    )
    with pytest.raises(PublishRefusedError, match="required step factory_floor"):
        HandlerFactoryFloorPublish(ssh).handle(request, environ={})
    assert ssh.ssh_calls == 0


def test_error_chain_the_refusal_block_is_kept_locally_so_the_next_refusal_finds_its_start(
    tmp_path: Path,
) -> None:
    _, site = _cycle(tmp_path, refuse=True, previous=PREVIOUS_STATUS)
    first = _served_status(site)["refused"]
    local = json.loads(
        (tmp_path / "home/floor-copy" / load_runtime().names.status).read_text()
    )
    assert local["refused"] == first
