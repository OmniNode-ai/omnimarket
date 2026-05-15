# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain proof-of-life test for node_dependency_health_sweep.

Three subtests:
  A. Clean fixture — temp dir with valid import chain + matched pub/sub → status=="clean"
  B. Finding fixture — orphan handler + unpaired command topic → status=="findings",
     findings include UNTESTED_HANDLER + MISSING_TOPIC_EDGE
  C. Event emission — EventBusInmemory receives dep-health-sweep-completed after handle()

Evidence bundle written to docs/evidence/dep-health-sweep/<run_id>/ by the test.
Topic constants loaded from contract.yaml, never hardcoded.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_dependency_health_sweep.handlers.handler_dep_health_sweep import (
    HandlerDepHealthSweep,
)
from omnimarket.nodes.node_dependency_health_sweep.models import (
    ModelDepHealthSweepRequest,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    EnumDepHealthFindingType,
)
from omnimarket.nodes.node_projection_dep_health.handlers.handler_projection_dep_health import (
    HandlerProjectionDepHealth,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

# ---------------------------------------------------------------------------
# Topic constants — loaded from contract.yaml, never hardcoded
# ---------------------------------------------------------------------------

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_dependency_health_sweep"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    with open(_CONTRACT_PATH) as fh:
        return yaml.safe_load(fh)  # type: ignore[no-any-return]


_CONTRACT = _load_contract()
_CMD_TOPIC: str = _CONTRACT["event_bus"]["subscribe_topics"][0]
_EVT_COMPLETED_TOPIC: str = next(
    t for t in _CONTRACT["event_bus"]["publish_topics"] if "sweep-completed" in t
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_CLEAN_CONTRACT_YAML = """\
name: node_clean_a
node_type: COMPUTE
event_bus:
  publish_topics:
    - "onex.evt.test.clean-done.v1"
  subscribe_topics:
    - "onex.cmd.test.clean-start.v1"
---
name: node_clean_b
node_type: EFFECT
event_bus:
  publish_topics:
    - "onex.cmd.test.clean-start.v1"
  subscribe_topics:
    - "onex.evt.test.clean-done.v1"
"""

_FINDING_CONTRACT_YAML = """\
name: node_orphan_handler
node_type: EFFECT
handler:
  module: omnimarket.nodes.node_orphan_handler.handlers.handler_orphan_handler
  class: HandlerOrphanHandler
event_bus:
  publish_topics:
    - "onex.cmd.test.orphan-cmd.v1"
  subscribe_topics: []
"""


def _write_clean_fixture(tmp_path: Path) -> None:
    """Temp dir with matched pub/sub contract.yaml and no Python source files.

    No Python files → no dead-import findings.
    Two contracts with symmetric pub/sub → no orphan-topic findings.
    No handler references → no untested-handler findings.
    """
    # Two contract.yaml files per node dir — fully symmetric pub/sub
    (tmp_path / "node_clean_a").mkdir(parents=True)
    (tmp_path / "node_clean_b").mkdir(parents=True)

    (tmp_path / "node_clean_a" / "contract.yaml").write_text("""\
name: node_clean_a
node_type: COMPUTE
event_bus:
  publish_topics:
    - "onex.evt.test.clean-done.v1"
  subscribe_topics:
    - "onex.cmd.test.clean-start.v1"
""")
    (tmp_path / "node_clean_b" / "contract.yaml").write_text("""\
name: node_clean_b
node_type: EFFECT
event_bus:
  publish_topics:
    - "onex.cmd.test.clean-start.v1"
  subscribe_topics:
    - "onex.evt.test.clean-done.v1"
""")


def _write_finding_fixture(tmp_path: Path) -> None:
    """Temp dir with orphan handler (no test) + unpaired command publish topic."""
    # Create handler directory referenced in contract
    handler_dir = (
        tmp_path / "src" / "omnimarket" / "nodes" / "node_orphan_handler" / "handlers"
    )
    handler_dir.mkdir(parents=True)
    (handler_dir / "handler_orphan_handler.py").write_text(
        "class HandlerOrphanHandler:\n    def handle(self, request: object) -> None:\n        pass\n"
    )
    (handler_dir / "__init__.py").write_text("")

    # contract.yaml: handler is referenced + command topic is published but no subscriber
    contract_dir = tmp_path / "src" / "omnimarket" / "nodes" / "node_orphan_handler"
    contract_dir.mkdir(parents=True, exist_ok=True)
    (contract_dir / "contract.yaml").write_text(_FINDING_CONTRACT_YAML)

    # No test files exist for handler_orphan_handler → UNTESTED_HANDLER fires
    # onex.cmd.test.orphan-cmd.v1 is published but no subscriber → MISSING_TOPIC_EDGE fires


# ---------------------------------------------------------------------------
# Evidence bundle writer
# ---------------------------------------------------------------------------


def _write_evidence_bundle(
    run_id: str,
    import_graph_data: dict[str, Any],
    topology_data: dict[str, Any],
    findings_data: list[dict[str, Any]],
    baseline_diff_data: dict[str, Any] | None,
    projection_rows: list[dict[str, Any]],
    subtest_outcomes: dict[str, str],
) -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    evidence_dir = repo_root / "docs" / "evidence" / "dep-health-sweep" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)

    (evidence_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "repo_roots": ["<fixture>"],
                "graphify_version": import_graph_data.get(
                    "graphify_version", "unknown"
                ),
                "rule_version": "v1",
            },
            indent=2,
        )
    )
    (evidence_dir / "import_graph.json").write_text(
        json.dumps(import_graph_data, indent=2)
    )
    (evidence_dir / "topology_graph.json").write_text(
        json.dumps(topology_data, indent=2)
    )
    (evidence_dir / "findings.json").write_text(json.dumps(findings_data, indent=2))
    (evidence_dir / "baseline_diff.json").write_text(
        json.dumps(baseline_diff_data, indent=2) if baseline_diff_data else "null"
    )

    # Serialize projection rows (convert non-JSON-safe values)
    safe_rows = []
    for row in projection_rows:
        safe_row = {}
        for k, v in row.items():
            safe_row[k] = (
                str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
            )
        safe_rows.append(safe_row)
    (evidence_dir / "projection_rows.json").write_text(json.dumps(safe_rows, indent=2))

    lines = [
        "# Dep-Health Sweep Proof-of-Life Evidence\n",
        f"run_id: {run_id}",
        f"timestamp: {datetime.now(UTC).isoformat()}",
        "",
        "## Subtest Outcomes",
        "",
    ]
    for name, outcome in subtest_outcomes.items():
        lines.append(f"- **{name}**: {outcome}")
    lines.append("")
    lines.append(f"## Findings count: {len(findings_data)}")
    lines.append(f"## Projection rows: {len(safe_rows)}")
    (evidence_dir / "proof_summary.md").write_text("\n".join(lines))

    return evidence_dir


# ---------------------------------------------------------------------------
# Golden chain tests
# ---------------------------------------------------------------------------

_RUN_ID_FOR_EVIDENCE = "proof-of-life-001"
_SUBTEST_OUTCOMES: dict[str, str] = {}
_EVIDENCE_DATA: dict[str, Any] = {
    "import_graph": {},
    "topology": {},
    "findings": [],
    "projection_rows": [],
}


@pytest.mark.unit
class TestGoldenChainDepHealthSweep:
    """Golden chain: handler invoke → findings → event emission → projection."""

    def test_a_clean_fixture(self, tmp_path: Path) -> None:
        """Clean fixture: valid import chain + matched pub/sub → status=='clean'."""
        _write_clean_fixture(tmp_path)

        handler = HandlerDepHealthSweep()
        result = handler.handle(
            ModelDepHealthSweepRequest(
                repo_roots=[str(tmp_path)],
                dry_run=True,
                run_id=f"{_RUN_ID_FOR_EVIDENCE}-clean",
            )
        )

        assert result.status == "clean", (
            f"Expected clean fixture to produce status='clean', got '{result.status}'. "
            f"Findings: {[f.model_dump() for f in result.findings]}"
        )

        _SUBTEST_OUTCOMES["A: clean_fixture"] = f"PASS — status={result.status}"
        _EVIDENCE_DATA["import_graph"] = {
            "graphify_version": result.graphify_version,
            "nodes": [],
            "edges": [],
            "orphan_modules": [],
        }
        _EVIDENCE_DATA["topology"] = {
            "nodes": [],
            "pub_edges": [],
            "sub_edges": [],
            "orphan_topics": [],
        }

    def test_b_finding_fixture(self, tmp_path: Path) -> None:
        """Finding fixture: orphan handler + unpaired cmd topic → findings."""
        _write_finding_fixture(tmp_path)

        handler = HandlerDepHealthSweep()
        result = handler.handle(
            ModelDepHealthSweepRequest(
                repo_roots=[str(tmp_path)],
                dry_run=False,
                run_id=f"{_RUN_ID_FOR_EVIDENCE}-findings",
            )
        )

        assert result.status == "findings", (
            f"Expected finding fixture to produce status='findings', got '{result.status}'."
        )
        assert len(result.findings) >= 2, (
            f"Expected at least 2 findings, got {len(result.findings)}: "
            f"{[f.model_dump() for f in result.findings]}"
        )

        finding_types = {f.finding_type for f in result.findings}
        assert EnumDepHealthFindingType.UNTESTED_HANDLER in finding_types, (
            f"Expected UNTESTED_HANDLER finding. Got: {finding_types}"
        )
        assert EnumDepHealthFindingType.MISSING_TOPIC_EDGE in finding_types, (
            f"Expected MISSING_TOPIC_EDGE finding. Got: {finding_types}"
        )

        _SUBTEST_OUTCOMES["B: finding_fixture"] = (
            f"PASS — status={result.status}, findings={len(result.findings)}, "
            f"types={sorted(str(ft) for ft in finding_types)}"
        )
        _EVIDENCE_DATA["findings"] = [
            f.model_dump(mode="json") for f in result.findings
        ]

    def test_c_event_emission(self, tmp_path: Path) -> None:
        """Event emission: EventBusInmemory receives sweep-completed event.

        The handler's _emit_completed_event uses asyncio.run() to publish.
        We start the bus in a separate asyncio.run() call first (each call
        gets its own event loop), then publish via the handler, then read
        history. Because EventBusInmemory stores state on the object (not
        on the loop), the three calls share state across loop boundaries.
        """
        _write_finding_fixture(tmp_path)

        bus = EventBusInmemory(environment="test", group="dep-health-test")

        # Start the bus in its own event loop
        asyncio.run(bus.start())

        # Handler.handle() calls asyncio.run() internally to publish;
        # this works because asyncio.run() spawns a new event loop and the
        # bus's internal asyncio.Lock is re-entered fresh per call.
        handler = HandlerDepHealthSweep(event_bus=bus)
        handler.handle(
            ModelDepHealthSweepRequest(
                repo_roots=[str(tmp_path)],
                dry_run=False,
                run_id=f"{_RUN_ID_FOR_EVIDENCE}-event",
            )
        )

        # Read history in its own event loop
        history = asyncio.run(bus.get_event_history(topic=_EVT_COMPLETED_TOPIC))
        asyncio.run(bus.close())

        assert len(history) >= 1, (
            f"Expected at least one event on topic {_EVT_COMPLETED_TOPIC!r}, "
            f"but bus history is empty."
        )

        _SUBTEST_OUTCOMES["C: event_emission"] = (
            f"PASS — topic={_EVT_COMPLETED_TOPIC!r}, events={len(history)}"
        )

    def test_d_projection_from_event(self, tmp_path: Path) -> None:
        """Projection: sweep-completed event rows project correctly into InmemoryDatabaseAdapter."""
        _write_finding_fixture(tmp_path)

        handler = HandlerDepHealthSweep()
        result = handler.handle(
            ModelDepHealthSweepRequest(
                repo_roots=[str(tmp_path)],
                dry_run=False,
                run_id=f"{_RUN_ID_FOR_EVIDENCE}-projection",
            )
        )

        from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_sweep_completed_event import (
            ModelDepHealthSweepCompletedEvent,
        )

        event = ModelDepHealthSweepCompletedEvent(
            run_id=result.run_id,
            findings=list(result.findings),
            summary=dict(result.summary),
            captured_at=datetime.now(UTC),
        )

        db = InmemoryDatabaseAdapter()
        projection_handler = HandlerProjectionDepHealth()
        proj_result = projection_handler.project(event, db)

        assert proj_result.rows_upserted == len(result.findings), (
            f"Expected {len(result.findings)} rows upserted, got {proj_result.rows_upserted}"
        )

        rows = db.query("dep_health_findings")
        assert len(rows) == len(result.findings)

        _SUBTEST_OUTCOMES["D: projection"] = (
            f"PASS — rows_upserted={proj_result.rows_upserted}"
        )
        _EVIDENCE_DATA["projection_rows"] = rows

        # Write evidence bundle on last subtest
        _write_evidence_bundle(
            run_id=_RUN_ID_FOR_EVIDENCE,
            import_graph_data=_EVIDENCE_DATA["import_graph"],
            topology_data=_EVIDENCE_DATA["topology"],
            findings_data=_EVIDENCE_DATA["findings"],
            baseline_diff_data=None,
            projection_rows=_EVIDENCE_DATA["projection_rows"],
            subtest_outcomes=_SUBTEST_OUTCOMES,
        )
