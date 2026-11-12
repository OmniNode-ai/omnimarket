# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The container-restarts condition of node_lab_alarm and its seam to the alarm delivery (OMN-23934).

Golden: every declared container inside its own bound reads OK and raises nothing. Error: a container past its bound
reads ALARM with one typed alarm per container, delivered once through the alarm delivery node and ended by the next OK.
INDETERMINATE: a container docker cannot answer for, a lane manifest that cannot be read, a lane that declares no bound,
never read as healthy, and dominating an alarm found on the containers that could be read. Seam: the alarm line splits
into the subject the delivery keys on; the real adapters read a lane manifest file and run a stand-in docker executable.
"""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryRestartBounds,
    InMemoryRestartReader,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    SshDockerRestartReader,
    YamlLaneManifestBounds,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_container_restarts import (
    CONDITION,
    HandlerContainerRestarts,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_container_restarts import (
    EnumContainerRestartsFault,
    EnumContainerRestartsOutcome,
    ModelContainerRestartsCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelContainerRestartsSettings,
    ModelLabAlarmOverlay,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    ContainerUnreadableError,
    RestartBoundsUnreadableError,
)
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import (
    build_container_restarts_handler,
    run_container_restarts,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CLOCK = datetime(2026, 11, 12, 11, 0, tzinfo=UTC)
_BOUNDS = {"runtime": 5, "broker": 5, "writer": 0}


def _settings() -> ModelContainerRestartsSettings:
    return ModelContainerRestartsSettings(
        lane_manifest="manifest.yaml",
        census_lane="dev",
        docker_command=("docker",),
        timeout_seconds=30,
    )


def _command(
    *,
    write: bool = True,
    fault: EnumContainerRestartsFault = EnumContainerRestartsFault.NONE,
) -> ModelContainerRestartsCommand:
    return ModelContainerRestartsCommand(
        correlation_id="restarts-1", write=write, fault=fault
    )


class _Rig:
    def __init__(
        self, bounds: dict[str, int] | None = None, *, deliver: bool = True
    ) -> None:
        self.bounds = InMemoryRestartBounds(_BOUNDS if bounds is None else bounds)
        self.reader = InMemoryRestartReader()
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.handler = HandlerContainerRestarts(
            settings=_settings(),
            bounds=self.bounds,
            reader=self.reader,
            delivery=delivery if deliver else None,
        )

    def healthy(self) -> None:
        self.reader.readings = {
            "runtime": (5, "running"),
            "broker": (0, "running"),
            "writer": (0, "running"),
        }


def test_container_restarts_every_container_inside_its_bound_is_ok_and_raises_nothing() -> (
    None
):
    rig = _Rig()
    rig.healthy()
    result = rig.handler.handle(_command())
    assert result.outcome is EnumContainerRestartsOutcome.OK
    assert result.alarms == ()
    assert result.failure == ""
    assert rig.reader.calls == ["broker", "runtime", "writer"], "read in name order"
    assert result.readings == (
        "broker=0/5 running",
        "runtime=5/5 running",
        "writer=0/0 running",
    )
    assert result.evidence.endswith("every container is inside its declared bound")
    assert rig.channel.posts == []
    assert rig.ledger.rows == []


def test_container_restarts_a_count_equal_to_the_bound_is_not_an_alarm() -> None:
    rig = _Rig({"runtime": 5})
    rig.reader.readings = {"runtime": (5, "running")}
    assert rig.handler.handle(_command()).outcome is EnumContainerRestartsOutcome.OK


def test_container_restarts_a_container_past_its_bound_is_one_alarm_naming_it() -> None:
    rig = _Rig()
    rig.healthy()
    rig.reader.readings["writer"] = (3, "restarting")
    result = rig.handler.handle(_command(write=False))
    assert result.outcome is EnumContainerRestartsOutcome.ALARM
    assert result.alarms == (
        "writer: container writer has restarted 3 times, past its declared bound of 0; "
        "status restarting",
    )
    assert result.failure == "alarm:container_restarts"
    assert result.census_lane == "dev"
    assert rig.ledger.rows == [], "a probe delivers nothing"


def test_container_restarts_each_container_past_its_bound_is_its_own_alarm() -> None:
    rig = _Rig()
    rig.healthy()
    rig.reader.readings["runtime"] = (6, "running")
    rig.reader.readings["broker"] = (9, "exited")
    result = rig.handler.handle(_command(write=False))
    assert [line.partition(": ")[0] for line in result.alarms] == ["broker", "runtime"]


def test_container_restarts_alarm_is_delivered_once_and_ok_ends_the_episode() -> None:
    rig = _Rig()
    rig.healthy()
    rig.reader.readings["writer"] = (3, "running")
    first = rig.handler.handle(_command())
    assert first.delivery == (f"{CONDITION}:writer channel=delivered ledger=delivered",)
    [(_, text)] = rig.channel.posts
    assert "writer" in text
    [(_, row)] = rig.ledger.rows
    assert "subject=writer" in row.split(" | ")
    again = rig.handler.handle(_command())
    assert again.delivery == (f"{CONDITION}:writer channel=suppressed ledger=not-due",)
    assert len(rig.channel.posts) == 1
    rig.healthy()
    assert rig.handler.handle(_command()).outcome is EnumContainerRestartsOutcome.OK
    rig.reader.readings["writer"] = (3, "running")
    rig.handler.handle(_command())
    assert len(rig.channel.posts) == 2


def test_container_restarts_a_failed_delivery_is_named_and_ends_as_a_failed_run() -> (
    None
):
    rig = _Rig()
    rig.healthy()
    rig.reader.readings["writer"] = (3, "running")
    rig.ledger.refuse = "ledger host unreachable"
    failed = rig.handler.handle(_command())
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure
    topics = load_topics()
    assert events_for(failed, topics)[-1][0] == topics["container_restarts_run_failed"]


def test_container_restarts_a_drill_alarms_without_reading_or_delivering() -> None:
    rig = _Rig()
    result = rig.handler.handle(
        _command(fault=EnumContainerRestartsFault.RESTARTS_OVER_BOUND)
    )
    assert result.outcome is EnumContainerRestartsOutcome.ALARM
    assert result.fault is EnumContainerRestartsFault.RESTARTS_OVER_BOUND
    [alarm] = result.alarms
    assert alarm.startswith("broker: container broker has restarted 6 times")
    assert rig.reader.calls == []
    assert result.delivery == ()
    assert rig.channel.posts == []
    assert rig.ledger.rows == []


def test_container_restarts_without_a_delivery_still_reads() -> None:
    rig = _Rig(deliver=False)
    rig.healthy()
    rig.reader.readings["writer"] = (3, "running")
    result = rig.handler.handle(_command())
    assert result.outcome is EnumContainerRestartsOutcome.ALARM
    assert result.delivery == ()


def test_container_restarts_golden_and_error_chain_events() -> None:
    topics = load_topics()
    rig = _Rig()
    rig.healthy()
    golden = events_for(rig.handler.handle(_command()), topics)
    assert [t for t, _ in golden] == [
        topics["container_restarts_run_started"],
        topics["container_restarts_run_completed"],
    ]
    rig.reader.readings["writer"] = (3, "running")
    error = events_for(rig.handler.handle(_command()), topics)
    assert [t for t, _ in error] == [
        topics["container_restarts_run_started"],
        topics["container_restarts_run_failed"],
    ]
    assert error[-1][1]["outcome"] == "ALARM"
    assert error[-1][1]["failure"] == "alarm:container_restarts"
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    for role in (
        "container_restarts_run_started",
        "container_restarts_run_completed",
        "container_restarts_run_failed",
    ):
        assert topics[role] in contract["externally_consumed_topics"]


def test_indeterminate_when_a_container_cannot_be_read_even_if_another_is_past_its_bound() -> (
    None
):
    rig = _Rig()
    rig.healthy()
    rig.reader.readings["runtime"] = (9, "running")
    del rig.reader.readings["broker"]
    rig.reader.unreadable["writer"] = "docker inspect writer exited 1: No such object"
    result = rig.handler.handle(_command())
    assert result.outcome is EnumContainerRestartsOutcome.INDETERMINATE
    assert result.alarms == (), "unreadable dominates a partial read"
    assert result.reasons == ("unreadable:broker", "unreadable:writer")
    assert "UNREADABLE: broker (broker is not a fixture container)" in result.evidence
    assert "writer (docker inspect writer exited 1: No such object)" in result.evidence
    assert result.failure == "indeterminate:unreadable:broker;unreadable:writer"
    assert rig.channel.posts == []
    assert rig.ledger.rows == []


def test_indeterminate_when_the_restart_bounds_cannot_be_read() -> None:
    rig = _Rig()
    rig.bounds.unreadable = "unknown census lane 'dev'"
    result = rig.handler.handle(_command())
    assert result.outcome is EnumContainerRestartsOutcome.INDETERMINATE
    assert result.reasons == ("bounds-unreadable",)
    assert "unknown census lane 'dev'" in result.evidence
    assert rig.reader.calls == [], "no container is read without its bound"


def test_indeterminate_when_no_container_carries_a_bound() -> None:
    rig = _Rig({})
    result = rig.handler.handle(_command())
    assert result.outcome is EnumContainerRestartsOutcome.INDETERMINATE
    assert result.reasons == ("no-bounds",)
    assert "an empty subject list is not a healthy lane" in result.evidence


def test_indeterminate_is_never_the_healthy_outcome() -> None:
    topics = load_topics()
    rig = _Rig({})
    result = rig.handler.handle(_command())
    assert events_for(result, topics)[-1][0] == topics["container_restarts_run_failed"]


def test_container_restarts_overlay_loads_without_the_block() -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert ModelLabAlarmOverlay.model_validate(raw).container_restarts is not None
    del raw["container_restarts"]
    assert ModelLabAlarmOverlay.model_validate(raw).container_restarts is None


def test_container_restarts_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del raw["container_restarts"]
    code = run_container_restarts(
        "container-restarts",
        EnumContainerRestartsFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
    )
    assert code == 1
    assert "no container_restarts block" in capsys.readouterr().err


def _example_overlay() -> ModelLabAlarmOverlay:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    return ModelLabAlarmOverlay.model_validate(raw)


def test_container_restarts_cli_exit_codes_follow_the_outcome() -> None:
    overlay = _example_overlay()
    settings = overlay.container_restarts
    assert settings is not None
    bounds = InMemoryRestartBounds({"runtime": 5})
    reader = InMemoryRestartReader()
    handler = HandlerContainerRestarts(settings=settings, bounds=bounds, reader=reader)
    lines: list[str] = []
    reader.readings["runtime"] = (6, "running")
    assert (
        run_container_restarts(
            "container-restarts-probe",
            EnumContainerRestartsFault.NONE,
            overlay=overlay,
            handler=handler,
            out=lines.append,
        )
        == 2
    )
    assert any(line.startswith("CONTAINER-RESTARTS outcome=ALARM") for line in lines)
    assert any(line.startswith("ALARM runtime: ") for line in lines)
    reader.readings["runtime"] = (5, "running")
    assert (
        run_container_restarts(
            "container-restarts",
            EnumContainerRestartsFault.NONE,
            overlay=overlay,
            handler=handler,
            out=lines.append,
        )
        == 0
    )
    reader.unreadable["runtime"] = "gone"
    assert (
        run_container_restarts(
            "container-restarts",
            EnumContainerRestartsFault.NONE,
            overlay=overlay,
            handler=handler,
            out=lines.append,
        )
        == 3
    )


def test_container_restarts_cli_exits_alarm_when_the_delivery_fails() -> None:
    rig = _Rig()
    rig.healthy()
    rig.reader.readings["writer"] = (3, "running")
    rig.ledger.refuse = "ledger host unreachable"
    code = run_container_restarts(
        "container-restarts",
        EnumContainerRestartsFault.NONE,
        overlay=_example_overlay(),
        handler=rig.handler,
        out=lambda _: None,
    )
    assert code == 2


def test_container_restarts_inmemory_backend_builds_the_fixture_sources() -> None:
    overlay = _example_overlay()
    assert overlay.container_restarts is not None
    handler = build_container_restarts_handler(overlay, overlay.container_restarts)
    assert handler.handle(_command()).reasons == ("no-bounds",)


def test_container_restarts_runtime_backend_builds_the_manifest_and_ssh_sources() -> (
    None
):
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    raw["bus"]["backend"] = "runtime"
    overlay = ModelLabAlarmOverlay.model_validate(raw)
    assert overlay.container_restarts is not None
    handler = build_container_restarts_handler(overlay, overlay.container_restarts)
    assert isinstance(handler._bounds, YamlLaneManifestBounds)
    assert isinstance(handler._reader, SshDockerRestartReader)


# --- the runtime adapters against a manifest file and a stand-in docker: the real adapters, no mocks ----------------


def _manifest(tmp_path: Path, lanes: object) -> Path:
    path = tmp_path / "lane-manifest.yaml"
    path.write_text(yaml.safe_dump({"lanes": lanes}), encoding="utf-8")
    return path


_LANES = {
    "dev": {
        "services": [
            {"name": "runtime", "restart_bound": 5},
            {"name": "writer", "restart_bound": 0},
            {"name": "no-bound", "kind": "service"},
        ]
    },
    "other": {"services": [{"name": "elsewhere", "restart_bound": 9}]},
}


def test_manifest_bounds_read_the_restart_bound_entries_of_the_census_lane(
    tmp_path: Path,
) -> None:
    path = _manifest(tmp_path, _LANES)
    bounds = YamlLaneManifestBounds(str(path), "dev", {}).bounds()
    assert bounds == {"runtime": 5, "writer": 0}


def test_manifest_bounds_resolve_the_workspace_root_variable(tmp_path: Path) -> None:
    _manifest(tmp_path, _LANES)
    source = YamlLaneManifestBounds(
        "${CONFIG_ROOT}/lane-manifest.yaml", "dev", {"CONFIG_ROOT": str(tmp_path)}
    )
    assert source.bounds() == {"runtime": 5, "writer": 0}


def test_indeterminate_manifest_bounds_refuse_an_unset_variable() -> None:
    with pytest.raises(
        RestartBoundsUnreadableError, match="unset variable CONFIG_ROOT"
    ):
        YamlLaneManifestBounds("${CONFIG_ROOT}/m.yaml", "dev", {}).bounds()


@pytest.mark.parametrize(
    ("lanes", "lane", "message"),
    [
        (_LANES, "absent", "unknown census lane 'absent'"),
        ({"dev": {"services": [{"name": "a"}]}}, "dev", "declares no restart_bound"),
        (
            {"dev": {"services": [{"name": "a", "restart_bound": -1}]}},
            "dev",
            "restart_bound must be a non-negative integer",
        ),
        (
            {"dev": {"services": [{"name": "a", "restart_bound": True}]}},
            "dev",
            "restart_bound must be a non-negative integer",
        ),
        (
            {
                "dev": {"services": [{"name": "a", "restart_bound": 1}]},
                "bad": {"services": [{"name": "b", "restart_bound": "x"}]},
            },
            "dev",
            "bad/b: restart_bound must be a non-negative integer",
        ),
    ],
)
def test_indeterminate_manifest_bounds_refuse_what_cannot_be_read_as_a_bound(
    tmp_path: Path, lanes: object, lane: str, message: str
) -> None:
    path = _manifest(tmp_path, lanes)
    with pytest.raises(RestartBoundsUnreadableError, match=message):
        YamlLaneManifestBounds(str(path), lane, {}).bounds()


def test_indeterminate_manifest_bounds_refuse_a_missing_or_unshaped_manifest(
    tmp_path: Path,
) -> None:
    with pytest.raises(RestartBoundsUnreadableError, match="unreadable"):
        YamlLaneManifestBounds(str(tmp_path / "absent.yaml"), "dev", {}).bounds()
    path = tmp_path / "flat.yaml"
    path.write_text("- not a mapping\n", encoding="utf-8")
    with pytest.raises(RestartBoundsUnreadableError, match="missing 'lanes'"):
        YamlLaneManifestBounds(str(path), "dev", {}).bounds()


def _docker(tmp_path: Path, body: str) -> tuple[str, ...]:
    script = tmp_path / "fake-docker"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return (str(script),)


def test_ssh_docker_reader_reads_the_count_and_status_the_inspect_prints(
    tmp_path: Path,
) -> None:
    command = _docker(tmp_path, 'echo "7 running"')
    assert SshDockerRestartReader(command).read("runtime", 5.0) == (7, "running")


def test_ssh_docker_reader_quotes_the_arguments_behind_a_remote_prefix(
    tmp_path: Path,
) -> None:
    log = tmp_path / "argv.txt"
    command = _docker(tmp_path, f'printf "%s\\n" "$@" > {log}; echo "0 running"')
    SshDockerRestartReader(command).read("run time", 5.0)
    assert log.read_text(encoding="utf-8").splitlines() == [
        "inspect",
        "-f",
        "'{{.RestartCount}} {{.State.Status}}'",
        "'run time'",
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('echo "no such object" >&2; exit 1', "exited 1: no such object"),
        ("echo ''", "printed"),
        ('echo "7"', "printed"),
        ('echo "many running"', "is not an integer"),
    ],
)
def test_indeterminate_ssh_docker_reader_never_returns_a_count_it_did_not_read(
    tmp_path: Path, body: str, message: str
) -> None:
    command = _docker(tmp_path, body)
    with pytest.raises(ContainerUnreadableError, match=message):
        SshDockerRestartReader(command).read("runtime", 5.0)


def test_indeterminate_ssh_docker_reader_names_a_docker_that_cannot_start(
    tmp_path: Path,
) -> None:
    with pytest.raises(ContainerUnreadableError, match="FileNotFoundError"):
        SshDockerRestartReader((str(tmp_path / "no-such-docker"),)).read("c", 5.0)


def test_indeterminate_ssh_docker_reader_names_a_docker_that_times_out(
    tmp_path: Path,
) -> None:
    command = _docker(tmp_path, "sleep 5")
    with pytest.raises(ContainerUnreadableError, match="TimeoutExpired"):
        SshDockerRestartReader(command).read("c", 0.2)


def test_container_restarts_real_adapters_read_a_manifest_and_a_docker_end_to_end(
    tmp_path: Path,
) -> None:
    path = _manifest(tmp_path, _LANES)
    command = _docker(
        tmp_path,
        'for last; do :; done; case "$last" in *runtime*) echo "9 running";; *) echo "0 running";; esac',
    )
    handler = HandlerContainerRestarts(
        settings=_settings(),
        bounds=YamlLaneManifestBounds(str(path), "dev", {}),
        reader=SshDockerRestartReader(command),
    )
    result = handler.handle(_command())
    assert result.outcome is EnumContainerRestartsOutcome.ALARM
    assert [line.partition(": ")[0] for line in result.alarms] == ["runtime"]
