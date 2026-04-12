"""Golden chain test for node_coverage_sweep.

Verifies the handler can scan coverage data, identify gaps, classify
priorities, and emit completion events via EventBusInmemory.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_coverage_sweep.handlers.handler_coverage_sweep import (
    CoverageSweepRequest,
    NodeCoverageSweep,
)

CMD_TOPIC = "onex.cmd.omnimarket.coverage-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.coverage-sweep-completed.v1"


def _write_coverage_json(repo_dir: Path, files: dict[str, dict[str, object]]) -> None:
    """Write a coverage.json file with the given file data."""
    data = {"files": files}
    (repo_dir / "coverage.json").write_text(json.dumps(data))


@pytest.mark.unit
class TestCoverageSweepGoldenChain:
    """Golden chain: command -> handler -> completion event."""

    async def test_clean_repo_above_target(self, event_bus: EventBusInmemory) -> None:
        """Repos with all modules above target should produce status=clean."""
        handler = NodeCoverageSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _write_coverage_json(
                repo,
                {
                    "src/module_a.py": {
                        "summary": {
                            "percent_covered": 85.0,
                            "num_statements": 100,
                            "missing_lines": 15,
                        }
                    }
                },
            )

            request = CoverageSweepRequest(target_dirs=[tmpdir], target_pct=50.0)
            result = handler.handle(request)

        assert result.status == "clean"
        assert result.total_gaps == 0
        assert result.repos_scanned == 1
        assert result.total_modules == 1

    async def test_zero_coverage_detected(self, event_bus: EventBusInmemory) -> None:
        """Modules with 0% coverage should be flagged as ZERO priority."""
        handler = NodeCoverageSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _write_coverage_json(
                repo,
                {
                    "src/uncovered.py": {
                        "summary": {
                            "percent_covered": 0.0,
                            "num_statements": 50,
                            "missing_lines": 50,
                        }
                    }
                },
            )

            request = CoverageSweepRequest(target_dirs=[tmpdir])
            result = handler.handle(request)

        assert result.status == "gaps_found"
        assert result.total_gaps == 1
        assert result.by_priority.get("ZERO", 0) == 1
        assert result.zero_coverage == 1

    async def test_recently_changed_priority(self, event_bus: EventBusInmemory) -> None:
        """Recently changed modules below target get RECENTLY_CHANGED priority."""
        handler = NodeCoverageSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _write_coverage_json(
                repo,
                {
                    "src/changed.py": {
                        "summary": {
                            "percent_covered": 30.0,
                            "num_statements": 80,
                            "missing_lines": 56,
                        }
                    }
                },
            )

            request = CoverageSweepRequest(
                target_dirs=[tmpdir],
                recently_changed_modules=["src/changed.py"],
            )
            result = handler.handle(request)

        assert result.total_gaps == 1
        assert result.gaps[0].priority == "RECENTLY_CHANGED"
        assert result.gaps[0].recently_changed is True

    async def test_below_target_priority(self, event_bus: EventBusInmemory) -> None:
        """Modules below target but not zero and not recently changed get BELOW_TARGET."""
        handler = NodeCoverageSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _write_coverage_json(
                repo,
                {
                    "src/old.py": {
                        "summary": {
                            "percent_covered": 25.0,
                            "num_statements": 40,
                            "missing_lines": 30,
                        }
                    }
                },
            )

            request = CoverageSweepRequest(target_dirs=[tmpdir])
            result = handler.handle(request)

        assert result.total_gaps == 1
        assert result.gaps[0].priority == "BELOW_TARGET"

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = NodeCoverageSweep()
        results_captured: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            request = CoverageSweepRequest(
                target_dirs=payload.get("target_dirs", []),
                target_pct=payload.get("target_pct", 50.0),
            )
            result = handler.handle(request)
            result_payload = {
                "status": result.status,
                "total_gaps": result.total_gaps,
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
            CMD_TOPIC, on_message=on_command, group_id="test-coverage"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _write_coverage_json(
                repo,
                {
                    "src/ok.py": {
                        "summary": {
                            "percent_covered": 90.0,
                            "num_statements": 10,
                            "missing_lines": 1,
                        }
                    }
                },
            )
            cmd_payload = json.dumps({"target_dirs": [tmpdir]}).encode()
            await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["status"] == "clean"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_dry_run_flag(self, event_bus: EventBusInmemory) -> None:
        """dry_run flag should propagate from request to result."""
        handler = NodeCoverageSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _write_coverage_json(repo, {})
            request = CoverageSweepRequest(target_dirs=[tmpdir], dry_run=True)
            result = handler.handle(request)

        assert result.dry_run is True

    async def test_multiple_repos(self, event_bus: EventBusInmemory) -> None:
        """Scanning multiple repos aggregates gaps correctly."""
        handler = NodeCoverageSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("repo_a", "repo_b"):
                repo = Path(tmpdir) / name
                repo.mkdir()
                _write_coverage_json(
                    repo,
                    {
                        "src/low.py": {
                            "summary": {
                                "percent_covered": 10.0,
                                "num_statements": 50,
                                "missing_lines": 45,
                            }
                        }
                    },
                )

            request = CoverageSweepRequest(
                target_dirs=[str(Path(tmpdir) / "repo_a"), str(Path(tmpdir) / "repo_b")]
            )
            result = handler.handle(request)

        assert result.repos_scanned == 2
        assert result.total_gaps == 2

    async def test_missing_coverage_file(self, event_bus: EventBusInmemory) -> None:
        """Repos without coverage.json are scanned but produce no gaps."""
        handler = NodeCoverageSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            request = CoverageSweepRequest(target_dirs=[tmpdir])
            result = handler.handle(request)

        assert result.repos_scanned == 1
        assert result.total_modules == 0
        assert result.status == "clean"

    async def test_average_coverage_computed(self, event_bus: EventBusInmemory) -> None:
        """Average coverage should be computed across all modules."""
        handler = NodeCoverageSweep()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            _write_coverage_json(
                repo,
                {
                    "src/a.py": {
                        "summary": {
                            "percent_covered": 80.0,
                            "num_statements": 10,
                            "missing_lines": 2,
                        }
                    },
                    "src/b.py": {
                        "summary": {
                            "percent_covered": 40.0,
                            "num_statements": 10,
                            "missing_lines": 6,
                        }
                    },
                },
            )

            request = CoverageSweepRequest(target_dirs=[tmpdir])
            result = handler.handle(request)

        assert result.average_coverage == 60.0
        assert result.total_modules == 2
