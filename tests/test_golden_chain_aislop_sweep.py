"""Golden chain test for node_aislop_sweep.

Verifies the handler can be wired to EventBusInmemory, receive a command
event, execute the scan, and emit a completion event.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_aislop_sweep.handlers.handler_aislop_sweep import (
    AislopSweepRequest,
    ModelSweepFinding,
    NodeAislopSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.aislop-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.aislop-sweep-completed.v1"


@pytest.mark.unit
class TestAislopSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_clean_repo_produces_clean_result(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A repo with no anti-patterns should produce status=clean."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "clean.py").write_text("def hello():\n    return 42\n")

            request = AislopSweepRequest(target_dirs=[tmpdir])
            result = handler.handle(request)

        assert result.status == "clean"
        assert result.total_findings == 0
        assert result.repos_scanned == 1

    async def test_prohibited_pattern_detected(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Prohibited env var patterns should be flagged as CRITICAL."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "bad.py").write_text('ONEX_EVENT_BUS_TYPE = "inmemory"\n')

            request = AislopSweepRequest(
                target_dirs=[tmpdir], checks=["prohibited-patterns"]
            )
            result = handler.handle(request)

        assert result.status == "findings"
        assert result.total_findings >= 1
        assert result.by_severity.get("CRITICAL", 0) >= 1
        assert result.by_check.get("prohibited-patterns", 0) >= 1

    async def test_hardcoded_topic_detected(self, event_bus: EventBusInmemory) -> None:
        """Hardcoded topic strings in src/ should be flagged as ERROR."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "topics.py").write_text('TOPIC = "onex.evt.core.something.v1"\n')

            request = AislopSweepRequest(
                target_dirs=[tmpdir], checks=["hardcoded-topics"]
            )
            result = handler.handle(request)

        assert result.status == "findings"
        assert result.total_findings >= 1

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = NodeAislopSweep()
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            request = AislopSweepRequest(
                target_dirs=payload.get("target_dirs", []),
                checks=payload.get("checks"),
            )
            result = handler.handle(request)
            result_payload = {
                "status": result.status,
                "total_findings": result.total_findings,
                "repos_scanned": result.repos_scanned,
            }
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-aislop"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "ok.py").write_text("x = 1\n")

            cmd_payload = json.dumps({"target_dirs": [tmpdir]}).encode()
            await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["status"] == "clean"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_todo_detection(self, event_bus: EventBusInmemory) -> None:
        """TODO markers in src/ should be flagged."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "wip.py").write_text("# TODO: fix this later\nx = 1\n")

            request = AislopSweepRequest(target_dirs=[tmpdir], checks=["todo-fixme"])
            result = handler.handle(request)

        assert result.status == "findings"
        assert result.by_check.get("todo-fixme", 0) >= 1

    async def test_selective_checks(self, event_bus: EventBusInmemory) -> None:
        """Only specified checks should run."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "mixed.py").write_text(
                '# TODO fix\nONEX_EVENT_BUS_TYPE = "inmemory"\n'
            )

            # Only check todos, not prohibited patterns
            request = AislopSweepRequest(target_dirs=[tmpdir], checks=["todo-fixme"])
            result = handler.handle(request)

        assert "prohibited-patterns" not in result.by_check
        assert result.by_check.get("todo-fixme", 0) >= 1

    async def test_dry_run_flag_propagated(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag should propagate from request to result."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "ok.py").write_text("x = 1\n")

            request = AislopSweepRequest(target_dirs=[tmpdir], dry_run=True)
            result = handler.handle(request)

        assert result.dry_run is True
        assert result.status == "clean"

    async def test_compat_shims_detected(self, event_bus: EventBusInmemory) -> None:
        """Backwards-compat shims in src/ should be flagged as WARNING."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "old.py").write_text("x = 1  # removed\n_unused_var = None\n")

            request = AislopSweepRequest(target_dirs=[tmpdir], checks=["compat-shims"])
            result = handler.handle(request)

        assert result.status == "findings"
        assert result.by_check.get("compat-shims", 0) >= 2

    async def test_empty_impls_detected(self, event_bus: EventBusInmemory) -> None:
        """Empty pass statements in src/ should be flagged as WARNING."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "empty.py").write_text("def do_something():\n    pass\n")

            request = AislopSweepRequest(target_dirs=[tmpdir], checks=["empty-impls"])
            result = handler.handle(request)

        assert result.status == "findings"
        assert result.by_check.get("empty-impls", 0) >= 1

    async def test_empty_impls_skips_abstract_files(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Abstract/Protocol files should be exempt from empty-impls check."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "abstract_base.py").write_text("def do_something():\n    pass\n")

            request = AislopSweepRequest(target_dirs=[tmpdir], checks=["empty-impls"])
            result = handler.handle(request)

        assert result.status == "clean"

    async def test_ticketable_property(self, event_bus: EventBusInmemory) -> None:
        """Findings with HIGH confidence and severity >= WARNING are ticketable."""
        # CRITICAL + HIGH -> ticketable
        critical = ModelSweepFinding(
            repo="test",
            path="src/bad.py",
            line=1,
            check="prohibited-patterns",
            message="test",
            severity="CRITICAL",
            confidence="HIGH",
        )
        assert critical.ticketable is True

        # WARNING + MEDIUM -> not ticketable
        warning = ModelSweepFinding(
            repo="test",
            path="src/old.py",
            line=1,
            check="compat-shims",
            message="test",
            severity="WARNING",
            confidence="MEDIUM",
        )
        assert warning.ticketable is False

        # ERROR + HIGH -> ticketable
        error = ModelSweepFinding(
            repo="test",
            path="src/topic.py",
            line=1,
            check="hardcoded-topics",
            message="test",
            severity="ERROR",
            confidence="HIGH",
        )
        assert error.ticketable is True

    async def test_multiple_repos(self, event_bus: EventBusInmemory) -> None:
        """Scanning multiple repos aggregates findings correctly."""
        handler = NodeAislopSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_a = Path(tmpdir) / "repo_a"
            repo_b = Path(tmpdir) / "repo_b"
            for repo in (repo_a, repo_b):
                src = repo / "src"
                src.mkdir(parents=True)
                (src / "bad.py").write_text('ONEX_EVENT_BUS_TYPE = "inmemory"\n')

            request = AislopSweepRequest(
                target_dirs=[str(repo_a), str(repo_b)],
                checks=["prohibited-patterns"],
            )
            result = handler.handle(request)

        assert result.repos_scanned == 2
        assert result.total_findings == 2
        assert result.by_severity.get("CRITICAL", 0) == 2

    async def test_event_bus_finding_emission(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Full golden chain: command -> scan -> finding events -> completion event."""
        handler = NodeAislopSweep()
        finding_topic = "onex.evt.omnimarket.aislop-sweep-finding.v1"
        finding_events: list[dict[str, object]] = []
        completion_events: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            request = AislopSweepRequest(
                target_dirs=payload.get("target_dirs", []),
                checks=payload.get("checks"),
                dry_run=payload.get("dry_run", False),
            )
            result = handler.handle(request)

            # Emit per-finding events
            for finding in result.findings:
                finding_payload = finding.model_dump()
                finding_events.append(finding_payload)
                await event_bus.publish(
                    finding_topic,
                    key=None,
                    value=json.dumps(finding_payload).encode(),
                )

            # Emit completion event
            completion = {
                "status": result.status,
                "total_findings": result.total_findings,
                "repos_scanned": result.repos_scanned,
                "by_severity": result.by_severity,
                "by_check": result.by_check,
                "dry_run": result.dry_run,
            }
            completion_events.append(completion)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(completion).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-aislop-full"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = Path(tmpdir) / "src"
            src_dir.mkdir()
            (src_dir / "bad.py").write_text(
                'ONEX_EVENT_BUS_TYPE = "inmemory"\n# TODO: fix this\n'
            )

            cmd_payload = json.dumps(
                {
                    "target_dirs": [tmpdir],
                    "dry_run": True,
                }
            ).encode()
            await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        # Verify finding events emitted
        assert len(finding_events) >= 2
        checks_found = {f["check"] for f in finding_events}
        assert "prohibited-patterns" in checks_found
        assert "todo-fixme" in checks_found

        # Verify completion event
        assert len(completion_events) == 1
        assert completion_events[0]["status"] == "findings"
        assert completion_events[0]["dry_run"] is True

        # Verify event bus history
        finding_history = await event_bus.get_event_history(topic=finding_topic)
        assert len(finding_history) >= 2

        completion_history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(completion_history) == 1

        await event_bus.close()
