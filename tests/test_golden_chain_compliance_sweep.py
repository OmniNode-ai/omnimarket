"""Golden chain test for node_compliance_sweep.

Verifies the handler can scan handler files, detect contract compliance
violations, and emit completion events via EventBusInmemory.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_compliance_sweep.handlers.handler_compliance_sweep import (
    ComplianceSweepRequest,
    NodeComplianceSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.compliance-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.compliance-sweep-completed.v1"


@pytest.mark.unit
class TestComplianceSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_compliant_handler(self, event_bus: EventBusInmemory) -> None:
        """A handler with no violations should produce status=compliant."""
        handler = NodeComplianceSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            handlers_dir = Path(tmpdir) / "src" / "nodes" / "node_test" / "handlers"
            handlers_dir.mkdir(parents=True)
            (handlers_dir / "handler_test.py").write_text(
                "from pydantic import BaseModel\n\ndef handle():\n    return 42\n"
            )

            request = ComplianceSweepRequest(target_dirs=[tmpdir])
            result = handler.handle(request)

        assert result.status == "compliant"
        assert result.total_violations == 0
        assert result.handlers_scanned >= 1

    async def test_hardcoded_topic_detected(self, event_bus: EventBusInmemory) -> None:
        """Hardcoded topic strings in handlers should be flagged."""
        handler = NodeComplianceSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            handlers_dir = Path(tmpdir) / "src" / "nodes" / "node_test" / "handlers"
            handlers_dir.mkdir(parents=True)
            (handlers_dir / "handler_bad.py").write_text(
                'TOPIC = "onex.evt.core.something.v1"\n'
            )

            request = ComplianceSweepRequest(
                target_dirs=[tmpdir], checks=["hardcoded-topics"]
            )
            result = handler.handle(request)

        assert result.status == "violations_found"
        assert result.by_type.get("HARDCODED_TOPIC", 0) >= 1

    async def test_transport_import_detected(self, event_bus: EventBusInmemory) -> None:
        """Transport library imports in handlers should be flagged."""
        handler = NodeComplianceSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            handlers_dir = Path(tmpdir) / "src" / "nodes" / "node_test" / "handlers"
            handlers_dir.mkdir(parents=True)
            (handlers_dir / "handler_db.py").write_text("import psycopg2\n\nx = 1\n")

            request = ComplianceSweepRequest(
                target_dirs=[tmpdir], checks=["undeclared-transport"]
            )
            result = handler.handle(request)

        assert result.status == "violations_found"
        assert result.by_type.get("UNDECLARED_TRANSPORT", 0) >= 1

    async def test_selective_checks(self, event_bus: EventBusInmemory) -> None:
        """Only specified checks should run."""
        handler = NodeComplianceSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            handlers_dir = Path(tmpdir) / "src" / "nodes" / "node_test" / "handlers"
            handlers_dir.mkdir(parents=True)
            (handlers_dir / "handler_mixed.py").write_text(
                'import httpx\nTOPIC = "onex.evt.core.foo.v1"\n'
            )

            request = ComplianceSweepRequest(
                target_dirs=[tmpdir], checks=["hardcoded-topics"]
            )
            result = handler.handle(request)

        assert "UNDECLARED_TRANSPORT" not in result.by_type
        assert result.by_type.get("HARDCODED_TOPIC", 0) >= 1

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = NodeComplianceSweep()
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            request = ComplianceSweepRequest(
                target_dirs=payload.get("target_dirs", []),
                checks=payload.get("checks"),
            )
            result = handler.handle(request)
            result_payload = {
                "status": result.status,
                "total_violations": result.total_violations,
                "handlers_scanned": result.handlers_scanned,
            }
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-compliance"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            handlers_dir = Path(tmpdir) / "src" / "nodes" / "node_ok" / "handlers"
            handlers_dir.mkdir(parents=True)
            (handlers_dir / "handler_ok.py").write_text("x = 1\n")
            cmd_payload = json.dumps({"target_dirs": [tmpdir]}).encode()
            await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_dry_run_flag(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag should propagate from request to result."""
        handler = NodeComplianceSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            request = ComplianceSweepRequest(target_dirs=[tmpdir], dry_run=True)
            result = handler.handle(request)

        assert result.dry_run is True

    async def test_compliant_vs_imperative_counts(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Compliant and imperative counts should sum to handlers_scanned."""
        handler = NodeComplianceSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            handlers_dir = Path(tmpdir) / "src" / "nodes" / "node_test" / "handlers"
            handlers_dir.mkdir(parents=True)
            (handlers_dir / "handler_clean.py").write_text("x = 1\n")
            (handlers_dir / "handler_dirty.py").write_text(
                'TOPIC = "onex.evt.core.bar.v1"\n'
            )

            request = ComplianceSweepRequest(target_dirs=[tmpdir])
            result = handler.handle(request)

        assert result.compliant + result.imperative == result.handlers_scanned

    async def test_by_severity_counts(self, event_bus: EventBusInmemory) -> None:
        """Severity counts should match total violations."""
        handler = NodeComplianceSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            handlers_dir = Path(tmpdir) / "src" / "nodes" / "node_test" / "handlers"
            handlers_dir.mkdir(parents=True)
            (handlers_dir / "handler_bad.py").write_text(
                'import httpx\nTOPIC = "onex.evt.core.x.v1"\n'
            )

            request = ComplianceSweepRequest(target_dirs=[tmpdir])
            result = handler.handle(request)

        total_severity = sum(result.by_severity.values())
        assert total_severity == result.total_violations
