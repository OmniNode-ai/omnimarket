# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for NodeFullTriageOrchestrator (OMN-9322 sub-1)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest
from omnibase_core.models.triage import (
    EnumProbeStatus,
    EnumTriageBlastRadius,
    EnumTriageFreshness,
    EnumTriageSeverity,
    ModelTriageFinding,
    ModelTriageProbeResult,
    ModelTriageReport,
)

from omnimarket.nodes.node_full_triage_orchestrator.handlers.handler_full_triage import (
    ModelFullTriageRequest,
    NodeFullTriageOrchestrator,
)
from omnimarket.nodes.node_full_triage_orchestrator.handlers.output_formatter import (
    report_to_json,
    report_to_json_dict,
    report_to_markdown,
)
from omnimarket.nodes.node_full_triage_orchestrator.handlers.probe_interface import (
    Probe,
)


@dataclass
class _FakeProbe:
    """In-memory probe fixture for deterministic orchestrator tests."""

    probe_name: str
    findings: list[ModelTriageFinding] = field(default_factory=list)
    status: EnumProbeStatus = EnumProbeStatus.SUCCESS
    duration_ms: int = 1
    sleep_s: float = 0.0
    raise_exc: Exception | None = None

    def run(self, timeout_s: float) -> ModelTriageProbeResult:
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if self.raise_exc is not None:
            raise self.raise_exc
        return ModelTriageProbeResult(
            probe_name=self.probe_name,
            status=self.status,
            findings=self.findings,
            duration_ms=self.duration_ms,
        )


def _finding(
    probe: str,
    severity: EnumTriageSeverity = EnumTriageSeverity.MEDIUM,
    message: str = "m",
    blast: EnumTriageBlastRadius = EnumTriageBlastRadius.REPO,
    freshness: EnumTriageFreshness = EnumTriageFreshness.LIVE,
) -> ModelTriageFinding:
    return ModelTriageFinding(
        source_probe=probe,
        severity=severity,
        freshness=freshness,
        blast_radius=blast,
        message=message,
    )


class TestProbeInterface:
    def test_fake_probe_satisfies_protocol(self) -> None:
        """_FakeProbe must satisfy the structural Probe protocol."""
        fp = _FakeProbe(probe_name="p")
        assert isinstance(fp, Probe)


class TestOrchestratorEmpty:
    def test_empty_probe_list_yields_empty_report(self) -> None:
        handler = NodeFullTriageOrchestrator()
        report = handler.handle(ModelFullTriageRequest())
        assert report.probe_results == []
        assert report.ranked_findings == []
        assert report.run_id.startswith("triage-")


class TestOrchestratorAggregation:
    def test_collects_findings_from_all_probes(self) -> None:
        probes = [
            _FakeProbe(
                probe_name="p_low",
                findings=[_finding("p_low", EnumTriageSeverity.LOW)],
            ),
            _FakeProbe(
                probe_name="p_crit",
                findings=[_finding("p_crit", EnumTriageSeverity.CRITICAL)],
            ),
        ]
        report = NodeFullTriageOrchestrator().handle(
            ModelFullTriageRequest(probes=probes)
        )
        assert len(report.probe_results) == 2
        assert len(report.ranked_findings) == 2
        assert report.ranked_findings[0].severity == EnumTriageSeverity.CRITICAL

    def test_probe_results_sorted_deterministically(self) -> None:
        probes = [
            _FakeProbe(probe_name="zeta"),
            _FakeProbe(probe_name="alpha"),
            _FakeProbe(probe_name="mid"),
        ]
        report = NodeFullTriageOrchestrator().handle(
            ModelFullTriageRequest(probes=probes)
        )
        assert [r.probe_name for r in report.probe_results] == [
            "alpha",
            "mid",
            "zeta",
        ]

    def test_custom_run_id_honored(self) -> None:
        report = NodeFullTriageOrchestrator().handle(
            ModelFullTriageRequest(probes=[], run_id="fixed-id")
        )
        assert report.run_id == "fixed-id"


class TestOrchestratorErrorHandling:
    def test_probe_exception_becomes_error_status(self) -> None:
        probes = [
            _FakeProbe(
                probe_name="healthy",
                findings=[_finding("healthy")],
            ),
            _FakeProbe(
                probe_name="broken",
                raise_exc=RuntimeError("boom"),
            ),
        ]
        report = NodeFullTriageOrchestrator().handle(
            ModelFullTriageRequest(probes=probes)
        )
        broken = next(r for r in report.probe_results if r.probe_name == "broken")
        healthy = next(r for r in report.probe_results if r.probe_name == "healthy")
        assert broken.status == EnumProbeStatus.ERROR
        assert "boom" in broken.error_message
        assert healthy.status == EnumProbeStatus.SUCCESS
        # Healthy probe's finding is still ranked
        assert len(report.ranked_findings) == 1
        assert report.ranked_findings[0].source_probe == "healthy"


class TestOrchestratorParallelism:
    def test_parallel_fanout_does_not_serialize(self) -> None:
        """Five probes each sleeping 0.2s must finish in <<1s total with fan-out."""
        probes = [_FakeProbe(probe_name=f"p_{i}", sleep_s=0.2) for i in range(5)]
        start = time.perf_counter()
        report = NodeFullTriageOrchestrator().handle(
            ModelFullTriageRequest(probes=probes, max_workers=5, probe_timeout_s=5.0)
        )
        elapsed = time.perf_counter() - start
        assert len(report.probe_results) == 5
        serial_total = sum(p.sleep_s for p in probes)  # ~1.0s
        assert elapsed < serial_total * 0.85, (
            f"fan-out appears serialized (elapsed={elapsed:.2f}s, serial={serial_total:.2f}s)"
        )


class TestOutputFormatters:
    def _sample_report(self) -> ModelTriageReport:
        probes = [
            _FakeProbe(
                probe_name="p1",
                findings=[
                    _finding(
                        "p1",
                        severity=EnumTriageSeverity.HIGH,
                        message="needs | escaping",
                    )
                ],
            ),
            _FakeProbe(probe_name="p2", raise_exc=ValueError("bad")),
        ]
        return NodeFullTriageOrchestrator().handle(
            ModelFullTriageRequest(probes=probes, run_id="fixture")
        )

    def test_markdown_contains_summary_and_findings(self) -> None:
        report = self._sample_report()
        md = report_to_markdown(report)
        assert "# Triage report — fixture" in md
        assert "## Severity summary" in md
        assert "## Probe execution" in md
        assert "## Ranked findings" in md
        assert "`p1`" in md
        # Pipe in message must be escaped
        assert r"needs \| escaping" in md
        # Error probe surfaced
        assert "p2" in md
        assert "ERROR" in md

    def test_markdown_no_findings_placeholder(self) -> None:
        report = NodeFullTriageOrchestrator().handle(
            ModelFullTriageRequest(probes=[], run_id="empty")
        )
        md = report_to_markdown(report)
        assert "_No findings._" in md

    def test_json_is_parseable_and_stable(self) -> None:
        report = self._sample_report()
        js = report_to_json(report)
        import json

        parsed = json.loads(js)
        assert parsed["run_id"] == "fixture"
        assert "ranked_findings" in parsed
        assert "probe_results" in parsed

    def test_json_dict_roundtrip(self) -> None:
        report = self._sample_report()
        d = report_to_json_dict(report)
        assert d["run_id"] == "fixture"
        assert isinstance(d["ranked_findings"], list)


@pytest.mark.unit
class TestDeterministicSnapshot:
    def test_same_inputs_produce_same_ranked_list(self) -> None:
        probes = [
            _FakeProbe(
                probe_name="p1",
                findings=[_finding("p1", EnumTriageSeverity.CRITICAL, message="a")],
            ),
            _FakeProbe(
                probe_name="p2",
                findings=[_finding("p2", EnumTriageSeverity.CRITICAL, message="a")],
            ),
            _FakeProbe(
                probe_name="p1b",
                findings=[_finding("p1b", EnumTriageSeverity.CRITICAL, message="a")],
            ),
        ]
        reports = [
            NodeFullTriageOrchestrator().handle(
                ModelFullTriageRequest(probes=probes, run_id=f"r{i}")
            )
            for i in range(3)
        ]
        ordered_probes = [[f.source_probe for f in r.ranked_findings] for r in reports]
        assert ordered_probes[0] == ordered_probes[1] == ordered_probes[2]
        # Alphabetical by source_probe when severity/freshness/blast all tied
        assert ordered_probes[0] == ["p1", "p1b", "p2"]
