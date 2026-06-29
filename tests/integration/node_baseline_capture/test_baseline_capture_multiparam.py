# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_baseline_capture (OMN-13680, WS5 Wave 6).

Variant A — direct in-process handler call. The probe I/O boundary is satisfied
by injected mock probes implementing ``ProbeProtocol`` (the constructor
``probe_registry`` seam). No subprocess / asyncpg / live infra is touched.

Each parametrized case exercises a distinct probe-registry composition and
asserts typed result fields (``probes_run`` / ``probes_failed`` / snapshot
contents / artifact written-or-not). The ``probe-raises`` case is the negative
control: a known-bad probe must land in ``probes_failed`` while the rest still
capture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
    HandlerBaselineCapture,
    ModelBaselineCaptureRequest,
    ProbeProtocol,
)
from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelGitHubPRSnapshot,
    ModelServiceHealthSnapshot,
    ProbeSnapshotItem,
)


class _MockOkProbe:
    """Probe stub that returns a fixed item list (the success boundary)."""

    def __init__(self, name: str, items: list[ProbeSnapshotItem]) -> None:
        self.name = name
        self._items = items

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        return list(self._items)


class _MockFailProbe:
    """Probe stub that raises — proves the non-fatal failure path runs."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        raise RuntimeError(f"probe {self.name} infrastructure failure")


def _pr(num: int) -> ModelGitHubPRSnapshot:
    return ModelGitHubPRSnapshot(
        pr_number=num,
        title=f"PR {num}",
        repo="OmniNode-ai/omnimarket",
        state="open",
        age_days=1.0,
    )


def _svc(name: str, healthy: bool) -> ModelServiceHealthSnapshot:
    return ModelServiceHealthSnapshot(service=name, healthy=healthy)


def _registry(spec: dict[str, Any]) -> dict[str, ProbeProtocol]:
    registry: dict[str, ProbeProtocol] = {}
    for name, kind in spec.items():
        if kind == "fail":
            registry[name] = _MockFailProbe(name)
        else:
            registry[name] = _MockOkProbe(name, kind)
    return registry


# (registry_spec, request_probes, expect) tuples.
_CASES = [
    pytest.param(
        {BaselineProbeType.GITHUB_PRS: [_pr(1), _pr(2)]},
        [BaselineProbeType.GITHUB_PRS],
        {"run": {"github_prs"}, "failed": set(), "item_count": 2},
        id="single-probe-success",
    ),
    pytest.param(
        {
            BaselineProbeType.GITHUB_PRS: [_pr(1)],
            BaselineProbeType.SYSTEM_HEALTH: [_svc("api", True)],
        },
        [BaselineProbeType.GITHUB_PRS, BaselineProbeType.SYSTEM_HEALTH],
        {"run": {"github_prs", "system_health"}, "failed": set(), "item_count": 2},
        id="multi-probe-success",
    ),
    pytest.param(
        {
            BaselineProbeType.GITHUB_PRS: [_pr(1)],
            BaselineProbeType.SYSTEM_HEALTH: "fail",
        },
        [BaselineProbeType.GITHUB_PRS, BaselineProbeType.SYSTEM_HEALTH],
        {"run": {"github_prs"}, "failed": {"system_health"}, "item_count": 1},
        id="negative-control-probe-raises",
    ),
    pytest.param(
        {BaselineProbeType.GITHUB_PRS: [_pr(1)]},
        [BaselineProbeType.GITHUB_PRS, "not_a_real_probe"],
        {"run": {"github_prs"}, "failed": set(), "item_count": 1},
        id="unknown-probe-name-skipped",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("registry_spec", "request_probes", "expect"), _CASES)
@pytest.mark.asyncio
async def test_baseline_capture_multiparam(
    tmp_path: Path,
    registry_spec: dict[str, Any],
    request_probes: list[str],
    expect: dict[str, Any],
) -> None:
    handler = HandlerBaselineCapture(probe_registry=_registry(registry_spec))
    output_path = tmp_path / "baseline.json"
    request = ModelBaselineCaptureRequest(
        baseline_id="ci-multiparam",
        probes=request_probes,
        omni_home=str(tmp_path),
        output_path=str(output_path),
        dry_run=False,
    )

    result = await handler.handle(request)

    assert set(result.probes_run) == expect["run"]
    assert set(result.probes_failed) == expect["failed"]
    captured = sum(len(items) for items in result.snapshot.probes.values())
    assert captured == expect["item_count"]
    # Real artifact write proof: file exists and round-trips.
    assert result.dry_run is False
    assert output_path.exists()
    assert result.artifact_path == str(output_path)
    # Failed probes never leak into the snapshot.
    for failed in expect["failed"]:
        assert failed not in result.snapshot.probes


@pytest.mark.integration
@pytest.mark.asyncio
async def test_baseline_capture_dry_run_writes_no_artifact(tmp_path: Path) -> None:
    """dry_run=True returns the snapshot but writes no artifact to disk."""
    handler = HandlerBaselineCapture(
        probe_registry=_registry({BaselineProbeType.GITHUB_PRS: [_pr(7)]})
    )
    output_path = tmp_path / "baseline.json"
    result = await handler.handle(
        ModelBaselineCaptureRequest(
            baseline_id="ci-dry",
            probes=[BaselineProbeType.GITHUB_PRS],
            omni_home=str(tmp_path),
            output_path=str(output_path),
            dry_run=True,
        )
    )

    assert result.dry_run is True
    assert not output_path.exists()
    assert result.probes_run == ["github_prs"]
    assert len(result.snapshot.probes["github_prs"]) == 1
