# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_baseline_compare (OMN-13680, WS5 Wave 6).

Variant A — direct in-process handler call. The baseline artifact is a mock
on-disk snapshot written under ``tmp_path``; the "current" state is injected via
``current_snapshot`` so no probes (and no live infra) run. Each case asserts the
typed per-probe delta structure. The ``missing-baseline`` case is the negative
control: an absent artifact must produce an error result with ``error`` set
rather than raising.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelBaselineSnapshot,
    ModelDbRowCountDelta,
    ModelDbRowCountSnapshot,
    ModelGitHubPRDelta,
    ModelGitHubPRSnapshot,
    ModelServiceHealthDelta,
    ModelServiceHealthSnapshot,
    ProbeSnapshotItem,
)
from omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare import (
    HandlerBaselineCompare,
    ModelBaselineCompareRequest,
)

_T0 = datetime(2026, 6, 1, tzinfo=UTC)


def _pr(num: int, state: str = "open") -> ModelGitHubPRSnapshot:
    return ModelGitHubPRSnapshot(
        pr_number=num,
        title=f"PR {num}",
        repo="OmniNode-ai/omnimarket",
        state=state,
        age_days=1.0,
    )


def _snapshot(
    baseline_id: str, probes: dict[str, list[ProbeSnapshotItem]]
) -> ModelBaselineSnapshot:
    return ModelBaselineSnapshot(
        baseline_id=baseline_id, captured_at=_T0, probes=probes
    )


def _write_baseline(path: Path, snapshot: ModelBaselineSnapshot) -> None:
    path.write_text(snapshot.model_dump_json(), encoding="utf-8")


# (probe_name, baseline_probes, current_probes, assert_fn)
def _assert_no_drift(delta: Any) -> None:
    assert isinstance(delta, ModelGitHubPRDelta)
    assert delta.opened == []
    assert delta.closed == []
    assert delta.merged == []


def _assert_pr_opened(delta: Any) -> None:
    assert isinstance(delta, ModelGitHubPRDelta)
    assert delta.opened == [2]
    assert delta.merged == []


def _assert_db_growth(delta: Any) -> None:
    assert isinstance(delta, ModelDbRowCountDelta)
    assert delta.grown == ["events"]
    assert delta.row_delta_by_table["events"] == 10


def _assert_service_degraded(delta: Any) -> None:
    assert isinstance(delta, ModelServiceHealthDelta)
    assert delta.degraded == ["api"]
    assert delta.recovered == []


_CASES = [
    pytest.param(
        BaselineProbeType.GITHUB_PRS,
        {BaselineProbeType.GITHUB_PRS: [_pr(1)]},
        {BaselineProbeType.GITHUB_PRS: [_pr(1)]},
        _assert_no_drift,
        id="no-drift-identical",
    ),
    pytest.param(
        BaselineProbeType.GITHUB_PRS,
        {BaselineProbeType.GITHUB_PRS: [_pr(1)]},
        {BaselineProbeType.GITHUB_PRS: [_pr(1), _pr(2)]},
        _assert_pr_opened,
        id="negative-control-pr-opened",
    ),
    pytest.param(
        BaselineProbeType.DB_ROW_COUNTS,
        {
            BaselineProbeType.DB_ROW_COUNTS: [
                ModelDbRowCountSnapshot(table_name="events", row_count=10)
            ]
        },
        {
            BaselineProbeType.DB_ROW_COUNTS: [
                ModelDbRowCountSnapshot(table_name="events", row_count=20)
            ]
        },
        _assert_db_growth,
        id="db-row-growth",
    ),
    pytest.param(
        BaselineProbeType.SYSTEM_HEALTH,
        {
            BaselineProbeType.SYSTEM_HEALTH: [
                ModelServiceHealthSnapshot(service="api", healthy=True)
            ]
        },
        {
            BaselineProbeType.SYSTEM_HEALTH: [
                ModelServiceHealthSnapshot(service="api", healthy=False)
            ]
        },
        _assert_service_degraded,
        id="service-degraded",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("probe_name", "baseline_probes", "current_probes", "assert_fn"), _CASES
)
@pytest.mark.asyncio
async def test_baseline_compare_multiparam(
    tmp_path: Path,
    probe_name: str,
    baseline_probes: dict[str, list[ProbeSnapshotItem]],
    current_probes: dict[str, list[ProbeSnapshotItem]],
    assert_fn: Any,
) -> None:
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(baseline_path, _snapshot("pre-deploy", baseline_probes))

    handler = HandlerBaselineCompare()
    result = await handler.handle(
        ModelBaselineCompareRequest(
            baseline_id="pre-deploy",
            probes=[probe_name],
            omni_home=str(tmp_path),
            baseline_path=str(baseline_path),
            current_snapshot=_snapshot("pre-deploy__current", current_probes),
            dry_run=True,
        )
    )

    assert result.error is None
    assert result.baseline_captured_at == _T0
    assert probe_name in result.delta.per_probe_deltas
    assert_fn(result.delta.per_probe_deltas[probe_name])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_baseline_compare_missing_artifact_is_error(tmp_path: Path) -> None:
    """Negative control: an absent baseline artifact yields a typed error result."""
    handler = HandlerBaselineCompare()
    result = await handler.handle(
        ModelBaselineCompareRequest(
            baseline_id="does-not-exist",
            omni_home=str(tmp_path),
            baseline_path=str(tmp_path / "nope.json"),
            dry_run=True,
        )
    )

    assert result.error is not None
    assert "not found" in result.error
    assert result.summary.startswith("ERROR:")
    assert result.delta.per_probe_deltas == {}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_baseline_compare_writes_delta_artifact(tmp_path: Path) -> None:
    """Non-dry-run writes a delta artifact that round-trips to JSON."""
    baseline_path = tmp_path / "baseline.json"
    _write_baseline(
        baseline_path,
        _snapshot("pre-deploy", {BaselineProbeType.GITHUB_PRS: [_pr(1)]}),
    )
    handler = HandlerBaselineCompare()
    result = await handler.handle(
        ModelBaselineCompareRequest(
            baseline_id="pre-deploy",
            probes=[BaselineProbeType.GITHUB_PRS],
            omni_home=str(tmp_path),
            baseline_path=str(baseline_path),
            current_snapshot=_snapshot(
                "cur", {BaselineProbeType.GITHUB_PRS: [_pr(1), _pr(2)]}
            ),
            dry_run=False,
        )
    )

    assert result.dry_run is False
    report = Path(result.report_path)
    assert report.exists()
    parsed = json.loads(report.read_text(encoding="utf-8"))
    assert parsed["baseline_id"] == "pre-deploy"
