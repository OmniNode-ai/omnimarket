# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""stability_pin tests: the pin guards, the no-op refresh, the host overlay and the health read (OMN-21613)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_stability_lane_refresh_effect.adapters import (
    RecordingExecutor,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.models import (
    EnumStabilityRefreshOutcome as Outcome,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.models import (
    ModelLaneHealthReading,
    ModelReleasePinSet,
    ModelRunOutcome,
    ModelStabilityRefreshCommand,
)
from tests.nodes.node_stability_lane_refresh_effect.stability_support import (
    ALL_TAGS,
    OTHER,
    PIN,
    PIN_TEXT,
    Rig,
    overlay,
)

pytestmark = pytest.mark.unit


def command(**changes: object) -> ModelStabilityRefreshCommand:
    values: dict[str, object] = {"correlation_id": "c1"}
    values.update(changes)
    return ModelStabilityRefreshCommand.model_validate(values)


def test_stability_pin_differs_without_bump_exits_nonzero_and_runs_nothing() -> None:
    rig = Rig()
    result = rig.handler.handle(command(requested=OTHER, execute=True))
    assert result.outcome is Outcome.REFUSED_PIN_DIFFERS
    assert result.exit_code != 0
    assert rig.runner.calls == []


def test_stability_pin_differs_with_bump_runs_the_requested_set_and_marks_the_pin_stale() -> (
    None
):
    rig = Rig()
    result = rig.handler.handle(command(requested=OTHER, execute=True, bump=True))
    assert result.outcome is Outcome.REFRESHED
    assert result.pin_record_stale is True
    assert rig.store.rendered
    assert "omnibase_infra: v1.2.4" in rig.store.rendered[0]
    assert rig.runner.calls[0][0][2] == "memory://bump-pin-1"
    assert rig.store.applied == OTHER


def test_stability_pin_already_running_exits_zero_and_recreates_nothing() -> None:
    rig = Rig(applied=PIN)
    result = rig.handler.handle(command(execute=True))
    assert result.outcome is Outcome.ALREADY_PINNED
    assert result.exit_code == 0
    assert rig.runner.calls == []
    assert rig.runner.executed == []


def test_stability_pin_not_applied_dry_run_plans_without_execute_flag() -> None:
    rig = Rig()
    result = rig.handler.handle(command())
    assert result.outcome is Outcome.PLANNED
    assert result.exit_code == 0
    assert rig.runner.executed == []
    assert rig.store.applied is None
    argv = rig.runner.calls[0][0]
    assert argv[:3] == (
        "/example/home/refresh.sh",
        "--pin-file",
        "/example/config/pin.yaml",
    )


def test_stability_pin_execute_records_the_applied_set() -> None:
    rig = Rig()
    result = rig.handler.handle(command(execute=True))
    assert result.outcome is Outcome.REFRESHED
    assert rig.runner.executed
    assert rig.store.applied == PIN


def test_stability_pin_failed_refresh_records_nothing_and_returns_its_code() -> None:
    rig = Rig(
        runner=RecordingExecutor(ModelRunOutcome(return_code=2, output_tail="no"))
    )
    result = rig.handler.handle(command(execute=True))
    assert result.outcome is Outcome.REFRESH_FAILED
    assert result.exit_code == 2
    assert rig.store.applied is None


@pytest.mark.parametrize(
    "bad", ["dev-20261030195247-1e3c83494587", "main", "1e3c83494587", "v1.2", "lab-1"]
)
def test_stability_pin_refuses_a_tag_that_is_not_a_release_tag(bad: str) -> None:
    rig = Rig(pin_text=PIN_TEXT.replace("omnimarket: v3.4.5", f"omnimarket: {bad}"))
    result = rig.handler.handle(command(execute=True))
    assert result.outcome is Outcome.REFUSED_NOT_RELEASE_TAG
    assert "release-tag-only" in result.detail
    assert rig.runner.calls == []


def test_stability_pin_requested_dev_tag_cannot_build_a_pin_set() -> None:
    with pytest.raises(ValidationError):
        ModelReleasePinSet(
            omnibase_infra="dev-20261030195247-1e3c83494587",
            omnibase_core="v2.3.4",
            omnimarket="v3.4.5",
        )


def test_stability_pin_refuses_a_release_tag_the_clone_does_not_hold() -> None:
    tags = ALL_TAGS - {("omnimarket", "v3.4.5")}
    rig = Rig(tags=tags)
    result = rig.handler.handle(command(execute=True))
    assert result.outcome is Outcome.REFUSED_NOT_RELEASE_TAG
    assert "omnimarket has no tag v3.4.5" in result.detail
    assert rig.runner.calls == []


def test_stability_pin_ignores_a_retired_compat_line() -> None:
    rig = Rig()
    result = rig.handler.handle(command())
    assert dict(result.tags) == {
        "omnibase_infra": "v1.2.3",
        "omnibase_core": "v2.3.4",
        "omnimarket": "v3.4.5",
    }


def test_stability_pin_for_another_lane_is_refused() -> None:
    rig = Rig(pin_text=PIN_TEXT.replace("lane: example-lane", "lane: other-lane"))
    result = rig.handler.handle(command())
    assert result.outcome is Outcome.REFUSED_PIN_UNREADABLE
    assert rig.runner.calls == []


def test_stability_pin_unreadable_pin_is_refused() -> None:
    from omnimarket.nodes.node_stability_lane_refresh_effect.adapters import (
        UnreadablePinSource,
    )

    rig = Rig()
    rig.handler._pin_source = UnreadablePinSource()
    result = rig.handler.handle(command())
    assert result.outcome is Outcome.REFUSED_PIN_UNREADABLE
    assert result.exit_code != 0


def test_stability_host_overlay_reaches_the_refresh_environment_by_name_and_value() -> (
    None
):
    rig = Rig()
    rig.handler.handle(command())
    env = rig.runner.calls[0][1]
    assert env["LANE_PROBE_HOST"] == "example.invalid"
    assert env["EXAMPLE_PORT"] == "15436"
    assert env["EXAMPLE_CAP"] == "1024M"
    assert env["EXAMPLE_GROUP"] == "example-box"
    assert env["ONEX_WORKSPACE_CONFIG_ROOT"] == "/example/config"
    assert env["OMNI_HOME"] == "/example/home"


def test_stability_host_overlay_refuses_a_secret_shaped_name_and_a_duplicate() -> None:
    with pytest.raises(ValidationError):
        overlay(caps={"EXAMPLE_API_TOKEN": "x"})
    with pytest.raises(ValidationError):
        overlay(ports={"EXAMPLE_PORT": 1}, caps={"EXAMPLE_PORT": "2"})


def test_stability_second_host_overlay_changes_only_the_host_values() -> None:
    first = Rig()
    first.handler.handle(command())
    second = Rig()
    second.handler._overlay = overlay(
        box_id="other-box", probe_host="other.invalid", ports={"EXAMPLE_PORT": 25436}
    )
    second.handler.handle(command())
    one, two = first.runner.calls[0][1], second.runner.calls[0][1]
    assert {k for k in one if one[k] != two.get(k)} == {
        "LANE_PROBE_HOST",
        "EXAMPLE_PORT",
    }
    assert first.runner.calls[0][0] == second.runner.calls[0][0]


def test_stability_health_reads_healthy_for_the_pinned_version() -> None:
    rig = Rig(
        health=ModelLaneHealthReading(
            version="1.2.3", environment="example-env", wiring_healthy=True
        )
    )
    assert rig.handler.read_health("v1.2.3") == (True, "healthy")


@pytest.mark.parametrize(
    ("reading", "needle"),
    [
        (
            ModelLaneHealthReading(
                version="1.2.2", environment="example-env", wiring_healthy=True
            ),
            "version 1.2.2",
        ),
        (
            ModelLaneHealthReading(
                version="1.2.3", environment="dev", wiring_healthy=True
            ),
            "environment dev",
        ),
        (
            ModelLaneHealthReading(
                version="1.2.3", environment="example-env", wiring_healthy=False
            ),
            "wiring not healthy",
        ),
        (None, "health unreadable"),
    ],
)
def test_stability_health_names_what_is_wrong(
    reading: ModelLaneHealthReading | None, needle: str
) -> None:
    ok, detail = Rig(health=reading).handler.read_health("v1.2.3")
    assert ok is False
    assert needle in detail
