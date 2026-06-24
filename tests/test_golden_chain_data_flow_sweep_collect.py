# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-internal-ip OMN-13552 reason="tests assert lane->.201 host resolution and the ssh target argv; literal host is the assertion subject, not a shipping default"
# onex-allow-file OMN-13552 reason="tests assert lane->.201 host resolution and the ssh target argv; literal host is the assertion subject, not a shipping default"
"""Tests for the data_flow_sweep --collect path and LiveMetadataCollector.

OMN-12221: metadata collection (rpk/psql probes) moved from the skill into the
node's collector.py.  The handler remains pure; the collector is side-effectful
and lives in __main__.py / collector.py.

OMN-13552: the collector now probes a *targeted lane* (``ModelLaneTarget``)
instead of "whatever host this process runs on". Remote lanes (dev/stability/
prod/judge on .201) are reached over ``ssh <host> docker exec``; the local lane
keeps in-stack docker/psql. An unreachable target lane fails LOUD (status=error)
rather than mislabelling every flow PRODUCER_DOWN.

These tests verify:
1. collect_flow_metadata() returns a valid ModelFlowInput when probes fail
   gracefully (all tools unavailable in CI).
2. __main__.py _collect_live() falls back to stubs on per-flow probe failure.
3. --collect flag is wired through main() without crashing.
4. The handler still receives correct input regardless of collection path.
5. Lane resolution maps lanes to remote endpoints and the collector does NOT
   assume a local Docker broker for a remote lane (OMN-13552 regression).
6. An unreachable target lane surfaces status=error / exit 2, never a false
   PRODUCER_DOWN / false-clean verdict (OMN-13552 DoD).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnimarket.nodes.node_data_flow_sweep.handlers.handler_data_flow_sweep import (
    DataFlowSweepRequest,
    EnumFlowStatus,
    EnumProducerStatus,
    ModelFlowInput,
    NodeDataFlowSweep,
)
from omnimarket.nodes.node_data_flow_sweep.lane_target import (
    LaneResolutionError,
    ModelLaneTarget,
    resolve_lane_target,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub(
    topic: str = "onex.evt.test.stub.v1",
    handler_name: str = "projectStub",
    table_name: str = "stub_table",
) -> ModelFlowInput:
    return ModelFlowInput(topic=topic, handler_name=handler_name, table_name=table_name)


def _local_target() -> ModelLaneTarget:
    return resolve_lane_target("local")


# ---------------------------------------------------------------------------
# Lane resolution (OMN-13552)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLaneResolution:
    def test_dev_lane_resolves_to_remote_host(self) -> None:
        """dev resolves to the .201 runtime host + unprefixed container names."""
        target = resolve_lane_target("dev")
        assert target.is_remote is True
        assert (
            target.runtime_host == "192.168.86.201"
        )  # onex-allow-internal-ip OMN-13552 reason="test asserts canonical lane host resolution"
        assert target.redpanda_container == "omnibase-infra-redpanda"
        assert target.postgres_container == "omnibase-infra-postgres"

    def test_prefixed_lanes_resolve_to_prefixed_containers(self) -> None:
        for lane, prefix in (
            ("stability-test", "omnibase-infra-stability-test"),
            ("prod", "omnibase-infra-prod"),
            ("judge", "omnibase-infra-judge"),
        ):
            target = resolve_lane_target(lane)
            assert target.is_remote is True
            assert target.redpanda_container == f"{prefix}-redpanda"
            assert target.postgres_container == f"{prefix}-postgres"

    def test_stability_alias(self) -> None:
        assert resolve_lane_target("stability").lane == "stability-test"

    def test_local_lane_is_not_remote(self) -> None:
        target = resolve_lane_target("local")
        assert target.is_remote is False
        assert target.runtime_host == ""

    def test_runtime_host_override(self) -> None:
        target = resolve_lane_target("dev", runtime_host="10.0.0.5")
        assert target.runtime_host == "10.0.0.5"

    def test_unknown_lane_fails_loud(self) -> None:
        with pytest.raises(LaneResolutionError):
            resolve_lane_target("does-not-exist")


# ---------------------------------------------------------------------------
# Transport: remote lane probes go over SSH, never local docker (OMN-13552)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteTransport:
    def test_remote_lane_probe_uses_ssh_not_local_docker(self) -> None:
        """A remote lane probe must shell out to ssh, never a bare local docker.

        This is the core OMN-13552 regression: the collector must not assume a
        local Docker broker for a remote lane.
        """
        from omnimarket.nodes.node_data_flow_sweep import collector

        target = resolve_lane_target("dev")
        captured: list[list[str]] = []

        def fake_run_argv(argv: list[str], *, timeout: int) -> tuple[int, str]:
            captured.append(argv)
            return (
                0,
                "PARTITION  LEADER  EPOCH  REPLICAS  LOG-START  HIGH-WATERMARK\n0  1  0  [1]  0  3\n",
            )

        with patch.object(collector, "_run_argv", side_effect=fake_run_argv):
            status, _age = collector.probe_producer_status(target, "onex.evt.x.v1")

        assert status == EnumProducerStatus.ACTIVE
        assert captured, "no command was run"
        argv = captured[0]
        assert argv[0] == "ssh", f"remote lane must use ssh, got {argv!r}"
        assert (
            argv[1] == "jonah@192.168.86.201"
        )  # onex-allow-internal-ip OMN-13552 reason="test asserts ssh target host"
        assert "docker exec omnibase-infra-redpanda" in argv[2]

    def test_local_lane_probe_uses_bare_docker(self) -> None:
        """A local lane probe runs docker directly (no ssh prefix)."""
        from omnimarket.nodes.node_data_flow_sweep import collector

        target = resolve_lane_target("local")
        captured: list[list[str]] = []

        def fake_run_argv(argv: list[str], *, timeout: int) -> tuple[int, str]:
            captured.append(argv)
            return 1, "does not exist"

        with patch.object(collector, "_run_argv", side_effect=fake_run_argv):
            collector.probe_producer_status(target, "onex.evt.x.v1")

        assert captured[0][0] == "docker"
        assert "ssh" not in captured[0]


# ---------------------------------------------------------------------------
# Reachability preflight fails LOUD (OMN-13552 DoD)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReachabilityPreflight:
    def test_unreachable_lane_raises(self) -> None:
        from omnimarket.nodes.node_data_flow_sweep import collector

        target = resolve_lane_target("dev")
        with (
            patch.object(
                collector, "_run_argv", return_value=(255, "ssh: connect timeout")
            ),
            pytest.raises(collector.LaneUnreachableError),
        ):
            collector.assert_lane_reachable(target)

    def test_reachable_lane_does_not_raise(self) -> None:
        from omnimarket.nodes.node_data_flow_sweep import collector

        target = resolve_lane_target("dev")
        with patch.object(
            collector, "_run_argv", return_value=(0, "CLUSTER\nredpanda\n")
        ):
            collector.assert_lane_reachable(target)  # no raise


# ---------------------------------------------------------------------------
# collector.py unit tests (probes mocked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollector:
    def test_collect_flow_metadata_missing_topic(self) -> None:
        """When rpk reports topic missing, collector returns MISSING status."""
        from omnimarket.nodes.node_data_flow_sweep import collector

        with patch.object(
            collector, "_run_argv", return_value=(0, "Topic does not exist")
        ):
            result = collector.collect_flow_metadata(_stub(), _local_target())

        assert result.producer_status == EnumProducerStatus.MISSING
        assert result.consumer_lag == 0
        assert result.table_row_count == 0

    def test_collect_flow_metadata_empty_topic(self) -> None:
        """Topic exists but HW==0 => EMPTY (distinct from MISSING)."""
        from omnimarket.nodes.node_data_flow_sweep import collector

        describe = (
            "PARTITION  LEADER  EPOCH  REPLICAS  LOG-START-OFFSET  HIGH-WATERMARK\n"
            "0          1       0      [1]       0                 0\n"
        )
        with patch.object(collector, "_run_argv", return_value=(0, describe)):
            result = collector.collect_flow_metadata(_stub(), _local_target())

        assert result.producer_status == EnumProducerStatus.EMPTY

    def test_collect_flow_metadata_active_topic(self) -> None:
        """When rpk reports topic active, collector probes lag and row count."""
        from omnimarket.nodes.node_data_flow_sweep import collector

        def fake_docker_exec(
            target: ModelLaneTarget,
            container: str,
            inner_argv: list[str],
            *,
            timeout: int = 10,
        ) -> tuple[int, str]:
            joined = " ".join(inner_argv)
            if "topic describe" in joined:
                return 0, (
                    "PARTITION  LEADER  EPOCH  REPLICAS  LOG-START  HIGH-WATERMARK\n"
                    "0          1       0      [1]       0          5\n"
                )
            if "topic consume" in joined:
                return 1, ""  # age unavailable
            if "group describe" in joined:
                return 0, f"{_stub().topic}  0  0  0  0  0"
            if "COUNT" in joined:
                return 0, "42"
            if "INTERVAL" in joined:
                return 0, "1"
            return 1, ""

        with patch.object(collector, "_docker_exec", side_effect=fake_docker_exec):
            result = collector.collect_flow_metadata(_stub(), _local_target())

        assert result.producer_status == EnumProducerStatus.ACTIVE
        assert result.table_row_count == 42
        assert result.table_has_recent_data is True

    def test_collect_flow_metadata_handles_exception(self) -> None:
        """If a probe raises unexpectedly, collect_flow_metadata does not propagate."""
        from omnimarket.nodes.node_data_flow_sweep import collector

        with patch.object(collector, "_docker_exec", side_effect=RuntimeError("boom")):
            result = collector.collect_flow_metadata(_stub(), _local_target())

        assert result.producer_status == EnumProducerStatus.MISSING


# ---------------------------------------------------------------------------
# __main__.py _collect_live tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollectLive:
    def test_collect_live_falls_back_on_exception(self) -> None:
        """_collect_live() returns the stub unchanged when collector raises."""
        from omnimarket.nodes.node_data_flow_sweep.__main__ import _collect_live

        stubs = [_stub()]
        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.assert_lane_reachable",
                return_value=None,
            ),
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.collect_flow_metadata",
                side_effect=Exception("infra unavailable"),
            ),
        ):
            result = _collect_live(stubs, _local_target())

        assert len(result) == 1
        assert result[0].topic == stubs[0].topic

    def test_collect_live_returns_populated_flows(self) -> None:
        """_collect_live() returns collector output when probes succeed."""
        from omnimarket.nodes.node_data_flow_sweep.__main__ import _collect_live

        populated = ModelFlowInput(
            topic="onex.evt.test.stub.v1",
            handler_name="projectStub",
            table_name="stub_table",
            producer_status=EnumProducerStatus.ACTIVE,
            consumer_lag=0,
            table_row_count=99,
            table_has_recent_data=True,
        )
        stubs = [_stub()]
        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.assert_lane_reachable",
                return_value=None,
            ),
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.collect_flow_metadata",
                return_value=populated,
            ),
        ):
            result = _collect_live(stubs, _local_target())

        assert result[0].table_row_count == 99
        assert result[0].producer_status == EnumProducerStatus.ACTIVE

    def test_collect_live_propagates_unreachable(self) -> None:
        """_collect_live() lets LaneUnreachableError propagate (fail LOUD)."""
        from omnimarket.nodes.node_data_flow_sweep.__main__ import _collect_live
        from omnimarket.nodes.node_data_flow_sweep.collector import (
            LaneUnreachableError,
        )

        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector.assert_lane_reachable",
                side_effect=LaneUnreachableError("broker down"),
            ),
            pytest.raises(LaneUnreachableError),
        ):
            _collect_live([_stub()], resolve_lane_target("dev"))


# ---------------------------------------------------------------------------
# Handler still processes collected flows correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerWithCollectedFlows:
    def test_handler_classifies_collected_active_flow(self) -> None:
        handler = NodeDataFlowSweep()
        populated = ModelFlowInput(
            topic="onex.evt.test.collected.v1",
            handler_name="projectCollected",
            table_name="collected_table",
            producer_status=EnumProducerStatus.ACTIVE,
            consumer_lag=0,
            table_row_count=10,
            table_has_recent_data=True,
        )
        request = DataFlowSweepRequest(flows=[populated])
        result = handler.handle(request)

        assert result.flow_results[0].flow_status == EnumFlowStatus.FLOWING
        assert result.status == "healthy"

    def test_handler_classifies_collected_missing_flow(self) -> None:
        handler = NodeDataFlowSweep()
        populated = ModelFlowInput(
            topic="onex.evt.test.gone.v1",
            handler_name="projectGone",
            table_name="gone_table",
            producer_status=EnumProducerStatus.MISSING,
        )
        request = DataFlowSweepRequest(flows=[populated])
        result = handler.handle(request)

        assert result.flow_results[0].flow_status == EnumFlowStatus.PRODUCER_DOWN
        assert result.status == "issues_found"

    def test_handler_empty_flows_is_healthy(self) -> None:
        handler = NodeDataFlowSweep()
        result = handler.handle(DataFlowSweepRequest(flows=[]))
        assert result.status == "healthy"
        assert result.flows_checked == 0


# ---------------------------------------------------------------------------
# CLI integration — --collect / --lane wired through main()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMainCollectFlag:
    def test_main_collect_exits_zero_on_healthy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import sys

        from omnimarket.nodes.node_data_flow_sweep.__main__ import (
            _DEFAULT_FLOW_STUBS,
            main,
        )

        healthy_stub = ModelFlowInput(
            topic=_DEFAULT_FLOW_STUBS[0].topic,
            handler_name=_DEFAULT_FLOW_STUBS[0].handler_name,
            table_name=_DEFAULT_FLOW_STUBS[0].table_name,
            producer_status=EnumProducerStatus.ACTIVE,
            consumer_lag=0,
            table_row_count=5,
            table_has_recent_data=True,
        )

        monkeypatch.setattr(
            sys, "argv", ["node_data_flow_sweep", "--collect", "--lane", "dev"]
        )
        with patch(
            "omnimarket.nodes.node_data_flow_sweep.__main__._collect_live",
            return_value=[healthy_stub],
        ):
            main()

        out = capsys.readouterr().out
        import json

        result = json.loads(out)
        assert result["status"] == "healthy"
        assert result["healthy"] == 1

    def test_main_collect_exits_one_on_issues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        from omnimarket.nodes.node_data_flow_sweep.__main__ import (
            _DEFAULT_FLOW_STUBS,
            main,
        )

        broken_stub = ModelFlowInput(
            topic=_DEFAULT_FLOW_STUBS[0].topic,
            handler_name=_DEFAULT_FLOW_STUBS[0].handler_name,
            table_name=_DEFAULT_FLOW_STUBS[0].table_name,
            producer_status=EnumProducerStatus.MISSING,
        )

        monkeypatch.setattr(
            sys, "argv", ["node_data_flow_sweep", "--collect", "--lane", "dev"]
        )
        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.__main__._collect_live",
                return_value=[broken_stub],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1

    def test_main_collect_unreachable_lane_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An unreachable target lane => status=error, exit 2 (fail LOUD)."""
        import json
        import sys

        from omnimarket.nodes.node_data_flow_sweep.__main__ import main
        from omnimarket.nodes.node_data_flow_sweep.collector import (
            LaneUnreachableError,
        )

        monkeypatch.setattr(
            sys, "argv", ["node_data_flow_sweep", "--collect", "--lane", "dev"]
        )
        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.__main__._collect_live",
                side_effect=LaneUnreachableError("broker unreachable"),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 2
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "error"
        assert result["healthy"] == 0
        assert result["broken"] == 0

    def test_main_collect_unknown_lane_exits_two(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        from omnimarket.nodes.node_data_flow_sweep.__main__ import main

        monkeypatch.setattr(
            sys, "argv", ["node_data_flow_sweep", "--collect", "--lane", "bogus"]
        )
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 2

    def test_main_collect_and_flows_mutually_exclusive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        from omnimarket.nodes.node_data_flow_sweep.__main__ import main

        monkeypatch.setattr(
            sys,
            "argv",
            ["node_data_flow_sweep", "--collect", "--flows", "[]"],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()

        assert exc_info.value.code == 1
