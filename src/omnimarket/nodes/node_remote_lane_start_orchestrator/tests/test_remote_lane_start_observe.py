# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``start_mode: observe``: the start the node would make, published and written nowhere (OMN-23663)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostPoolReading,
    ModelLaneHostPoolSnapshot,
)
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_start_effect.adapters.adapter_scripted_transport import (
    ModelScriptedTransportScript,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_availability import (
    HostTableUnreadableError,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EnumRemoteLaneStartOutcome,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    Rig,
    request,
    settings,
)

pytestmark = pytest.mark.unit


def two_hosts() -> ModelLaneHostPoolSnapshot:
    """h201 is the better host (more free room, more free slots); h106 the other one the pool offers."""
    return ModelLaneHostPoolSnapshot(
        readings=tuple(
            ModelLaneHostPoolReading(
                name=name,
                target=name,
                engines=("claude_opus", "claude_sonnet"),
                lane_slots=slots,
                lane_cap=4,
                rank_free=rank,
                mem_avail_gb=16.0,
            )
            for name, slots, rank in (("h106", 1, 2.0), ("h201", 3, 9.0))
        )
    )


def test_observe_publishes_the_whole_start_and_writes_nothing(tmp_path: Path) -> None:
    """Observe places the lane, computes the whole start, and writes nothing else."""
    rig = Rig(
        tmp_path,
        cfg=settings(start_mode="observe"),
        login_labels=("example-label",),
    )
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.OBSERVED
    assert result.exit_code == 0
    observed = result.observed
    assert observed is not None
    assert observed.host == "h106"
    assert observed.lane == request().lane
    assert observed.key == request().key
    assert observed.ticket == request().ticket
    assert observed.run_id == request().run_id
    assert rig.host_calls() == []
    assert rig.ledger_rows() == []
    assert rig.attempted_rows("CLAIM") == []
    assert rig.receipts.written == []
    assert observed.account_label is None
    assert "host=h106" in observed.claim_cells
    assert "key=floor:alpha:alpha-t1" in observed.claim_cells
    assert request().model in observed.engine_argv
    assert f"<brief sha256:{observed.brief_sha256}>" in observed.engine_argv
    assert request().brief not in " ".join(observed.engine_argv)
    assert (
        observed.brief_sha256
        == hashlib.sha256(request().brief.encode("utf-8")).hexdigest()
    )
    assert observed.engine_environment_names == ("LANE_MODE",)
    assert "headless" not in observed.model_dump_json()


def test_observe_event_line_carries_the_same_start(tmp_path: Path) -> None:
    """The printed ``REMOTE-LANE-OBSERVED`` line parses to the observed start."""
    rig = Rig(
        tmp_path, cfg=settings(start_mode="observe"), login_labels=("example-label",)
    )
    result = rig.orchestrator.run(request())
    line = next(
        line for line in result.lines if line.startswith("REMOTE-LANE-OBSERVED ")
    )
    assert result.observed is not None
    assert json.loads(
        line[len("REMOTE-LANE-OBSERVED ") :]
    ) == result.observed.model_dump(mode="json")


def test_act_control_makes_the_calls_observe_does_not(tmp_path: Path) -> None:
    """The same rig in ``act`` makes the host calls and writes the CLAIM that observe does not."""
    rig = Rig(
        tmp_path,
        ModelScriptedTransportScript(),
        cfg=settings(),
        login_labels=("example-label",),
    )
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.ENDED
    assert rig.host_calls()
    assert len(rig.ledger_rows("CLAIM")) == 1


def test_observe_places_on_the_same_host_act_would_choose(tmp_path: Path) -> None:
    """Observe and act place the lane on the same host: the better of two, h201."""
    (tmp_path / "observe").mkdir()
    (tmp_path / "act").mkdir()
    observe = Rig(
        tmp_path / "observe",
        cfg=settings(start_mode="observe"),
        pool=two_hosts(),
        host_availability=lambda: {},
    )
    act = Rig(
        tmp_path / "act",
        ModelScriptedTransportScript(),
        cfg=settings(),
        pool=two_hosts(),
        host_availability=lambda: {},
    )
    observed = observe.orchestrator.run(request())
    acted = act.orchestrator.run(request())
    assert observed.observed is not None
    assert observed.observed.host == acted.host == "h201"


def test_a_host_the_table_marks_out_is_refused_with_its_reason(tmp_path: Path) -> None:
    """A host the table marks out is refused with its reason and nothing is written."""
    rig = Rig(
        tmp_path,
        cfg=settings(start_mode="observe"),
        login_labels=("example-label",),
        host_availability=lambda: {"h106": "down for repair"},
    )
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert result.exit_code == 75
    assert any(
        "unavailable in the host table: down for repair" in line
        for line in result.lines
    )
    assert rig.host_calls() == []
    assert rig.ledger_rows() == []


def test_the_same_host_in_service_is_placed(tmp_path: Path) -> None:
    """A host the table leaves in service is placed (the control for the refused case)."""
    rig = Rig(
        tmp_path,
        cfg=settings(start_mode="observe"),
        login_labels=("example-label",),
        host_availability=lambda: {},
    )
    result = rig.orchestrator.run(request())
    assert result.observed is not None
    assert result.observed.host == "h106"


def test_an_out_host_is_skipped_for_an_in_service_one(tmp_path: Path) -> None:
    """The better host, h201, is marked out: the lane goes to h106 and the event names h201's refusal."""
    rig = Rig(
        tmp_path,
        cfg=settings(start_mode="observe"),
        pool=two_hosts(),
        host_availability=lambda: {"h201": "away"},
    )
    result = rig.orchestrator.run(request())
    assert result.observed is not None
    assert result.observed.host == "h106"
    assert result.observed.refused_hosts == (
        "h201: admission: unavailable in the host table: away",
    )


def test_control_the_better_host_in_service_is_chosen(tmp_path: Path) -> None:
    """Failing control for the case above: with h201 in service the same pool places the lane on h201."""
    rig = Rig(
        tmp_path,
        cfg=settings(start_mode="observe"),
        pool=two_hosts(),
        host_availability=lambda: {},
    )
    result = rig.orchestrator.run(request())
    assert result.observed is not None
    assert result.observed.host == "h201"


def test_an_unreadable_host_table_refuses_the_start(tmp_path: Path) -> None:
    """A host table that cannot be read refuses the start with nothing written."""

    def unavailable() -> dict[str, str]:
        raise HostTableUnreadableError("table gone")

    rig = Rig(
        tmp_path,
        cfg=settings(start_mode="observe"),
        login_labels=("example-label",),
        host_availability=unavailable,
    )
    result = rig.orchestrator.run(request())
    assert result.outcome is EnumRemoteLaneStartOutcome.NO_HOST
    assert result.exit_code == 75
    assert any("host-table-unreadable" in line for line in result.lines)
    assert rig.host_calls() == []
    assert rig.ledger_rows() == []


def test_the_settings_refuse_an_unknown_start_mode() -> None:
    """The settings refuse a ``start_mode`` that is not ``act`` or ``observe``."""
    with pytest.raises(ValidationError):
        settings(start_mode="dry")
