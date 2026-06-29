# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_gap_compute (OMN-13680, WS5 Wave 6).

Variant A — direct in-process handler call. node_gap_compute is a pure COMPUTE
that scans ``contract.yaml`` files under explicit ``repo_roots``. The repo
snapshot is mocked as a synthetic contract tree under ``tmp_path``; no live
infra is touched (the live projection/migration/auth probes are recorded as
skipped, by design).

Coverage spans the main subcommands of the 23-arg surface: DETECT (clean,
findings, severity filter, repo filter, max-findings cap, blocked-when-no-roots)
and FIX/RECONCILE (classify a report artifact into AUTO/GATE dispatch classes).
The ``non-canonical-topic`` case is the negative control: a known-bad contract
must produce a CRITICAL finding.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_gap_compute.handlers.handler_gap_compute import (
    HandlerGapCompute,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
    EnumGapSeverityThreshold,
    EnumGapSubcommand,
    ModelGapComputeRequest,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_result import (
    EnumGapSeverity,
    EnumGapStatus,
)

# --- Synthetic contract fixtures (mock repo snapshot) ----------------------

_CLEAN_COMPUTE = """\
name: node_clean_compute
node_type: compute
"""

_CLEAN_EFFECT = """\
name: node_clean_effect
node_type: effect
event_bus:
  publish_topics:
    - onex.evt.omnimarket.clean-effect-done.v1
  subscribe_topics:
    - onex.cmd.omnimarket.clean-effect-start.v1
"""

# CRITICAL: effect node declaring a non-canonical topic string.
_BAD_TOPIC_EFFECT = """\
name: node_bad_topic
node_type: effect
event_bus:
  publish_topics:
    - not.a.canonical.topic
"""

# WARNING: node still flagged node_not_implemented.
_NOT_IMPLEMENTED = """\
name: node_unimplemented
node_type: effect
node_not_implemented: true
event_bus:
  publish_topics:
    - onex.evt.omnimarket.unimpl-done.v1
"""


def _make_repo(root: Path, contracts: dict[str, str]) -> Path:
    """Write {node_dir: contract_text} under a synthetic repo root."""
    repo = root / "omnimarket"
    for node_dir, text in contracts.items():
        node_path = repo / "src" / "omnimarket" / "nodes" / node_dir
        node_path.mkdir(parents=True, exist_ok=True)
        (node_path / "contract.yaml").write_text(text, encoding="utf-8")
    return repo


@pytest.mark.integration
def test_gap_detect_clean_tree(tmp_path: Path) -> None:
    """A canonical contract tree yields CLEAN with zero findings."""
    repo = _make_repo(
        tmp_path,
        {"node_clean_compute": _CLEAN_COMPUTE, "node_clean_effect": _CLEAN_EFFECT},
    )
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(
            subcommand=EnumGapSubcommand.DETECT, repo_roots=[str(repo)]
        )
    )
    assert result.status == EnumGapStatus.CLEAN
    assert result.findings == []
    assert result.contracts_checked == 2
    assert result.repos_in_scope == ["omnimarket"]


@pytest.mark.integration
def test_gap_detect_non_canonical_topic_is_critical(tmp_path: Path) -> None:
    """Negative control: a non-canonical topic must produce a CRITICAL finding."""
    repo = _make_repo(tmp_path, {"node_bad_topic": _BAD_TOPIC_EFFECT})
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(
            subcommand=EnumGapSubcommand.DETECT, repo_roots=[str(repo)]
        )
    )
    assert result.status == EnumGapStatus.FINDINGS
    assert result.finding_count >= 1
    crit = [f for f in result.findings if f.severity == EnumGapSeverity.CRITICAL]
    assert len(crit) == 1
    assert crit[0].rule_name == "topic_name_mismatch"
    assert crit[0].proof["topic"] == "not.a.canonical.topic"


@pytest.mark.integration
def test_gap_detect_node_not_implemented_warning(tmp_path: Path) -> None:
    """A node_not_implemented contract produces a WARNING MISSING_NODE_TYPE finding."""
    repo = _make_repo(tmp_path, {"node_unimplemented": _NOT_IMPLEMENTED})
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(
            subcommand=EnumGapSubcommand.DETECT, repo_roots=[str(repo)]
        )
    )
    assert result.status == EnumGapStatus.FINDINGS
    warns = [f for f in result.findings if f.severity == EnumGapSeverity.WARNING]
    assert any(f.rule_name == "node_not_implemented" for f in warns)


@pytest.mark.integration
def test_gap_detect_severity_threshold_filters_warnings(tmp_path: Path) -> None:
    """severity_threshold=CRITICAL drops the WARNING-only tree back to CLEAN."""
    repo = _make_repo(tmp_path, {"node_unimplemented": _NOT_IMPLEMENTED})
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(
            subcommand=EnumGapSubcommand.DETECT,
            repo_roots=[str(repo)],
            severity_threshold=EnumGapSeverityThreshold.CRITICAL,
        )
    )
    # Only CRITICAL survives the filter; the WARNING finding is removed -> CLEAN.
    assert result.findings == []
    assert result.status == EnumGapStatus.CLEAN


@pytest.mark.integration
def test_gap_detect_max_findings_cap(tmp_path: Path) -> None:
    """max_findings caps the returned finding list."""
    contracts = {
        f"node_bad_{i}": _BAD_TOPIC_EFFECT.replace("node_bad_topic", f"node_bad_{i}")
        for i in range(4)
    }
    repo = _make_repo(tmp_path, contracts)
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(
            subcommand=EnumGapSubcommand.DETECT,
            repo_roots=[str(repo)],
            max_findings=2,
        )
    )
    assert len(result.findings) == 2
    assert result.status == EnumGapStatus.FINDINGS


@pytest.mark.integration
def test_gap_detect_no_roots_is_blocked(tmp_path: Path) -> None:
    """An unresolvable repo root yields BLOCKED (non-clean), not a false CLEAN."""
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(
            subcommand=EnumGapSubcommand.DETECT,
            repo_roots=[str(tmp_path / "does_not_exist")],
        )
    )
    assert result.status == EnumGapStatus.BLOCKED
    assert any(p.probe == "intake" for p in result.skipped_probes)


@pytest.mark.integration
def test_gap_fix_classifies_report_into_dispatch_classes(tmp_path: Path) -> None:
    """FIX subcommand classifies report findings into AUTO/GATE dispatch counts."""
    report: dict[str, Any] = {
        "findings": [
            {
                "category": "CONTRACT_DRIFT",
                "boundary_kind": "kafka_topic",
                "rule_name": "topic_name_mismatch",
                "severity": "CRITICAL",
                "confidence": "DETERMINISTIC",
                "repos": ["omnimarket"],
                "message": "bad topic",
            },
            {
                "category": "MISSING_TEST",
                "boundary_kind": "test_coverage",
                "rule_name": "missing_unit_test",
                "severity": "WARNING",
                "confidence": "DETERMINISTIC",
                "repos": ["omnimarket"],
                "message": "no test",
            },
        ]
    }
    report_path = tmp_path / "gap-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(
            subcommand=EnumGapSubcommand.FIX, report=str(report_path)
        )
    )
    assert result.status == EnumGapStatus.FINDINGS
    # topic_name_mismatch -> AUTO; missing_unit_test -> GATE.
    assert result.dispatch_class_counts["AUTO"] == 1
    assert result.dispatch_class_counts["GATE"] == 1


@pytest.mark.integration
def test_gap_fix_without_report_is_blocked() -> None:
    """FIX with no report artifact is BLOCKED, not silently CLEAN."""
    result = HandlerGapCompute().handle(
        ModelGapComputeRequest(subcommand=EnumGapSubcommand.FIX)
    )
    assert result.status == EnumGapStatus.BLOCKED
    assert any(p.probe == "fix" for p in result.skipped_probes)
