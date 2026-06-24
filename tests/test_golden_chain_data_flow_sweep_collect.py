# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the data_flow_sweep --collect path and LiveMetadataCollector.

OMN-12221: metadata collection (rpk/psql probes) moved from the skill into the
node's collector.py.  The handler remains pure; the collector is side-effectful
and lives in __main__.py / collector.py.

These tests verify:
1. collect_flow_metadata() returns a valid ModelFlowInput when probes fail
   gracefully (all tools unavailable in CI).
2. __main__.py _collect_live() falls back to stubs on probe failure.
3. --collect flag is wired through main() without crashing.
4. The handler still receives correct input regardless of collection path.
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub(
    topic: str = "onex.evt.test.stub.v1",
    handler_name: str = "projectStub",
    table_name: str = "stub_table",
) -> ModelFlowInput:
    return ModelFlowInput(topic=topic, handler_name=handler_name, table_name=table_name)


# ---------------------------------------------------------------------------
# collector.py unit tests (probes mocked)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCollector:
    def test_collect_flow_metadata_missing_topic(self) -> None:
        """When rpk/kcat report topic missing, collector returns MISSING status."""
        from omnimarket.nodes.node_data_flow_sweep.collector import (
            collect_flow_metadata,
        )

        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.collector._run",
                return_value=(1, "unknown topic"),
            ),
        ):
            result = collect_flow_metadata(_stub())

        assert result.producer_status == EnumProducerStatus.MISSING
        assert result.consumer_lag == 0
        assert result.table_row_count == 0

    def test_collect_flow_metadata_active_topic(self) -> None:
        """When rpk reports topic active, collector probes lag and row count."""
        from omnimarket.nodes.node_data_flow_sweep.collector import (
            collect_flow_metadata,
        )

        def fake_run(cmd: str, *, timeout: int = 10) -> tuple[int, str]:
            if "topic describe" in cmd:
                # Return JSON-ish output with high_watermark > 0
                return 0, f'{{"name": "test", "high_watermark": 5}} {_stub().topic}'
            if "group describe" in cmd:
                return 0, f"{_stub().topic}  0  0  0  0  0"
            if "COUNT" in cmd:
                return 0, "42"
            if "INTERVAL" in cmd:
                return 0, "1"
            # kcat for age probe
            return 1, ""

        with patch(
            "omnimarket.nodes.node_data_flow_sweep.collector._run",
            side_effect=fake_run,
        ):
            result = collect_flow_metadata(_stub())

        assert result.producer_status == EnumProducerStatus.ACTIVE
        assert result.table_row_count == 42
        assert result.table_has_recent_data is True

    def test_collect_flow_metadata_handles_exception(self) -> None:
        """If _run raises unexpectedly, collect_flow_metadata does not propagate."""
        from omnimarket.nodes.node_data_flow_sweep.collector import (
            collect_flow_metadata,
        )

        with patch(
            "omnimarket.nodes.node_data_flow_sweep.collector._run",
            side_effect=RuntimeError("boom"),
        ):
            # Should not raise; treats topic as missing
            result = collect_flow_metadata(_stub())

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
        with patch(
            "omnimarket.nodes.node_data_flow_sweep.collector.collect_flow_metadata",
            side_effect=Exception("infra unavailable"),
        ):
            result = _collect_live(stubs)

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
        with patch(
            "omnimarket.nodes.node_data_flow_sweep.collector.collect_flow_metadata",
            return_value=populated,
        ):
            result = _collect_live(stubs)

        assert result[0].table_row_count == 99
        assert result[0].producer_status == EnumProducerStatus.ACTIVE


# ---------------------------------------------------------------------------
# Handler still processes collected flows correctly
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerWithCollectedFlows:
    def test_handler_classifies_collected_active_flow(self) -> None:
        """Handler correctly classifies a flow populated by the collector."""
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
        """Handler correctly classifies a flow whose topic was found MISSING."""
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

    def test_handler_empty_flows_resolves_default_stubs(self) -> None:
        """Empty flows (no collect) resolves the built-in critical-chain stubs.

        OMN-13534: previously ``flows=[]`` checked zero flows and returned
        ``healthy`` — a false-clean for the no-arg dispatch path. The handler
        now resolves the built-in stubs (zero-value defaults -> PRODUCER_DOWN),
        so a no-arg dispatch reports ``issues_found`` over real topology instead
        of silently passing over nothing.
        """
        handler = NodeDataFlowSweep()
        result = handler.handle(DataFlowSweepRequest(flows=[]))
        assert result.flows_checked == 3
        assert result.status == "issues_found"

    def test_handler_single_healthy_flow_is_healthy(self) -> None:
        """A single explicitly-supplied healthy flow returns healthy."""
        handler = NodeDataFlowSweep()
        healthy = ModelFlowInput(
            topic="onex.evt.test.ok.v1",
            handler_name="projectOk",
            table_name="ok_table",
            producer_status=EnumProducerStatus.ACTIVE,
            consumer_lag=0,
            table_row_count=5,
            table_has_recent_data=True,
        )
        result = handler.handle(DataFlowSweepRequest(flows=[healthy]))
        assert result.status == "healthy"
        assert result.flows_checked == 1


# ---------------------------------------------------------------------------
# CLI integration — --collect flag wired through main()
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMainCollectFlag:
    def test_main_collect_exits_zero_on_healthy(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main() with --collect returns normally (no SystemExit) when all flows are healthy."""
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

        monkeypatch.setattr(sys, "argv", ["node_data_flow_sweep", "--collect"])
        with patch(
            "omnimarket.nodes.node_data_flow_sweep.__main__._collect_live",
            return_value=[healthy_stub],
        ):
            # Healthy path — main() returns without calling sys.exit
            main()

        out = capsys.readouterr().out
        import json

        result = json.loads(out)
        assert result["status"] == "healthy"
        assert result["healthy"] == 1

    def test_main_collect_exits_one_on_issues(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() with --collect exits 1 when a flow is broken."""
        import sys

        from omnimarket.nodes.node_data_flow_sweep.__main__ import _DEFAULT_FLOW_STUBS

        broken_stub = ModelFlowInput(
            topic=_DEFAULT_FLOW_STUBS[0].topic,
            handler_name=_DEFAULT_FLOW_STUBS[0].handler_name,
            table_name=_DEFAULT_FLOW_STUBS[0].table_name,
            producer_status=EnumProducerStatus.MISSING,
        )

        from omnimarket.nodes.node_data_flow_sweep.__main__ import main

        monkeypatch.setattr(sys, "argv", ["node_data_flow_sweep", "--collect"])
        with (
            patch(
                "omnimarket.nodes.node_data_flow_sweep.__main__._collect_live",
                return_value=[broken_stub],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code == 1

    def test_main_collect_and_flows_mutually_exclusive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() exits 1 when both --collect and --flows are provided."""
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
