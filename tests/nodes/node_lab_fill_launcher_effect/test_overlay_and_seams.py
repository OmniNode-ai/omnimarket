# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay and the seams of the lab-fill launcher (OMN-24109).

Seams: (1) the version-1 result file the fill's status step writes and this judge reads; (2) lab-fill-latest.json this
node publishes and the SessionStart hook reads; (3) the one JSON line this entry prints and the morning tick reads. Each
has a test that fails when either side changes shape.
"""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_fire_outcome_compute.handlers.handler_lab_fill_fire_outcome import (
    HandlerLabFillFireOutcome,
)
from omnimarket.nodes.node_lab_fill_fire_outcome_compute.models import (
    ModelLabFillFireOutcomeRequest,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.models import (
    ModelLabFillLauncherResult,
)
from omnimarket.nodes.node_lab_fill_launcher_effect.overlay import (
    ModelLabFillLauncherOverlay,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers import HandlerLabFillStatus
from omnimarket.nodes.node_lab_fill_plan_compute.models import ModelLabFillStatusRequest

pytestmark = pytest.mark.unit

FIRE_ID = "20261113T060000Z-lab-fill"


def _example() -> dict[str, object]:
    text = (
        files("omnimarket.nodes.node_lab_fill_launcher_effect")
        .joinpath("overlay.example.yaml")
        .read_text("utf-8")
    )
    return yaml.safe_load(text)  # type: ignore[no-any-return]


def test_overlay_example_loads_through_the_overlay_model() -> None:
    overlay = ModelLabFillLauncherOverlay.model_validate(_example())
    assert overlay.policy.workflow == "lab-fill"
    assert overlay.state.latest_filename == "lab-fill-latest.json"


@pytest.mark.parametrize("block", ["state", "alert", "policy"])
def test_overlay_refuses_a_missing_block(block: str) -> None:
    document = _example()
    del document[block]
    with pytest.raises(ValidationError):
        ModelLabFillLauncherOverlay.model_validate(document)


def test_overlay_refuses_a_missing_policy_key() -> None:
    document = _example()
    policy = dict(document["policy"])  # type: ignore[call-overload]
    del policy["class_blind"]
    document["policy"] = policy
    with pytest.raises(ValidationError):
        ModelLabFillLauncherOverlay.model_validate(document)


def test_overlay_refuses_an_unknown_key() -> None:
    document = _example()
    document["surprise"] = 1
    with pytest.raises(ValidationError):
        ModelLabFillLauncherOverlay.model_validate(document)


def _status_idle(dispatched: int) -> dict[str, object]:
    """What the fill's status step (node_lab_fill_plan_compute, the writer of the version-1 result) builds."""
    request = {
        "config": {
            "pillar": "lab-fill",
            "parent_lane": "lab-fill",
            "parent_ticket": "OMN-17427",
            "run_key": "2026-11-13T0600Z",
            "authority_ruling": "2026-11-13T00:00:00Z",
            "authority_lane": "orchestrator-31d9",
        },
        "capacity": {
            "hosts": [
                {
                    "name": "h202",
                    "lanes": 3,
                    "idleLanes": 3,
                    "runnerSlots": 3,
                    "capBound": False,
                    "codex": True,
                    "claude": True,
                    "running": 0,
                    "loadPerCore": 0.1,
                    "reason": "ok",
                }
            ],
            "free": 3,
            "budget": 3,
        },
        "selection": {"budget": 3},
        "observed_at": "2026-11-13T06:00:00Z",
        "fire_id": FIRE_ID,
        "dispatched": [{"lane": "l1", "detached": True}] * dispatched,
    }
    return (
        HandlerLabFillStatus()
        .handle(ModelLabFillStatusRequest.model_validate(request))
        .idle
    )


def _judge(value: dict[str, object], **overrides: object):  # type: ignore[no-untyped-def]
    overlay = ModelLabFillLauncherOverlay.model_validate(_example())
    fields = {
        "policy": overlay.policy,
        "fire_id": FIRE_ID,
        "phase": "completed",
        "exit": 0,
        "result_channel_enabled": True,
        "workflow_result_file": "/x",
        "truncated_marker_seen": False,
        "file_state": "present",
        "file_nonempty": True,
        "file_text": json.dumps(value),
        "run_log_refused_line": "",
        "run_log_path": "/x.log",
        "latest_path": "/x/latest.json",
        "snapshot_name": "linear-2026-11-13.json",
        "snapshot_present": True,
    }
    fields.update(overrides)
    return HandlerLabFillFireOutcome().handle(ModelLabFillFireOutcomeRequest(**fields))  # type: ignore[arg-type]


def test_seam_result_file_the_status_step_writes_is_read_by_the_judge() -> None:
    """Seam 1, a fire that placed a lane: the writer's own output passes every rule the reader checks."""
    result = _judge(_status_idle(dispatched=1))
    assert (result.phase, result.degraded_class, result.alert) == (
        "completed",
        None,
        None,
    )
    assert result.latest_value is not None


def test_seam_result_file_an_idle_result_needs_the_friction_flag_the_writer_leaves_false() -> (
    None
):
    """Seam 1, an idle fire: the writer leaves friction_recorded false until the FRICTION row is written."""
    idle = _status_idle(dispatched=0)
    assert idle["idle_alarm"] is True
    assert idle["friction_recorded"] is False
    unrecorded = _judge(idle)
    assert (unrecorded.phase, unrecorded.degraded_class) == (
        "result_invalid",
        "lab-idle-friction-missing",
    )
    recorded = _judge({**idle, "friction_recorded": True})
    assert (
        recorded.phase,
        recorded.alert.category.value if recorded.alert else None,
    ) == ("completed", "lab_fill_idle")


def test_seam_result_file_a_writer_that_renames_a_key_is_caught() -> None:
    """Injected break: the writer drops idle_alarm; the reader must refuse the file."""
    broken = {k: v for k, v in _status_idle(dispatched=1).items() if k != "idle_alarm"}
    assert _judge(broken).phase == "result_invalid"


def test_seam_tick_line_keys_are_what_the_tick_reads() -> None:
    """Seam 3: morning_workflows_tick.sh reads exactly these keys from the entry's one line."""
    assert set(ModelLabFillLauncherResult.model_fields) == {
        "phase",
        "exit",
        "degraded_class",
        "result_channel",
        "result_truncated",
        "notice_line",
        "alert_exit",
        "alert_category",
    }
    line = ModelLabFillLauncherResult(
        phase="completed",
        exit=0,
        degraded_class=None,
        result_channel="read",
        result_truncated=False,
        notice_line="",
        alert_exit=None,
        alert_category=None,
    ).tick_line()
    assert "\n" not in line
    assert json.loads(line)["degraded_class"] is None


def test_seam_latest_file_keeps_the_keys_session_start_reads() -> None:
    """Seam 2: the SessionStart hook reads version, workflow, observed_at and session_line of the published file."""
    result = _judge(_status_idle(dispatched=1))
    assert result.latest_value is not None
    assert {"version", "workflow", "observed_at", "session_line"} <= set(
        result.latest_value
    )
