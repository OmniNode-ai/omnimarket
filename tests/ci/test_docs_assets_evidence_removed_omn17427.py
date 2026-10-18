# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: unreferenced docs assets and evidence live in the knowledge base.

The logo and wordmark set, the closed tickets' evidence records and the ADR
inventory under ``docs/`` have no reader in this repository, so they were archived
in the knowledge base and removed here. The files that a test, script
or workflow reads stay, and the second test keeps the first from passing on an
empty ``docs`` tree.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

REMOVED_DOCS_FILES = (
    "docs/adr-canary/adr-inventory.yaml",
    "docs/assets/brand/omninode-icon-black.png",
    "docs/assets/brand/omninode-icon-full-color.svg",
    "docs/assets/brand/omninode-icon-white.png",
    "docs/assets/brand/omninode-inline-black.png",
    "docs/assets/brand/omninode-inline-full-color.svg",
    "docs/assets/brand/omninode-inline-white.png",
    "docs/assets/brand/omninode-stacked-black.png",
    "docs/assets/brand/omninode-stacked-full-color.svg",
    "docs/assets/brand/omninode-stacked-white.png",
    "docs/assets/brand/omninode-wordmark-black.svg",
    "docs/assets/brand/omninode-wordmark-grey.svg",
    "docs/assets/brand/omninode-wordmark-white.svg",
    "docs/evidence/dep-health-sweep/proof-of-life-001/baseline_diff.json",
    "docs/evidence/dep-health-sweep/proof-of-life-001/findings.json",
    "docs/evidence/dep-health-sweep/proof-of-life-001/import_graph.json",
    "docs/evidence/dep-health-sweep/proof-of-life-001/projection_rows.json",
    "docs/evidence/dep-health-sweep/proof-of-life-001/run_manifest.json",
    "docs/evidence/dep-health-sweep/proof-of-life-001/topology_graph.json",
    "docs/evidence/doc-content-scan/doc-content-scan.generation.json",
    "docs/evidence/g2-mass-produce-provenance/hardcoded-localhost-url.generation.json",
    "docs/evidence/g2-mass-produce-provenance/hardcoded-private-ip.generation.json",
    "docs/evidence/g2-mass-produce-provenance/hardcoded-topic-string.generation.json",
    "docs/evidence/g2-mass-produce-provenance/negative-control.acceptance.json",
    "docs/evidence/g2-mass-produce-provenance/todo-fixme-marker.generation.json",
    "docs/evidence/OMN-12964/quality_gate_calibration_packet.json",
    "docs/evidence/OMN-17427-delegate-local-coder-lab.json",
    "docs/evidence/pin-hygiene/pin-hygiene.generation.json",
    "docs/evidence/pin-hygiene/pin-hygiene.reproduction.generation.json",
    "docs/evidence/premium-counterfactual/calibration_record.json",
    "docs/evidence/premium-counterfactual/projection_row_counterfactual.json",
    "docs/evidence/quality-gate-calibration/quality_gate_calibration_packet.json",
    "docs/evidence/routing-authority-proof/routing-authority-proof.json",
)

KEPT_BECAUSE_READ = (
    "docs/evidence/OMN-15423-relation-inventory.json",
    "docs/evidence/OMN-17013-deployed-bus-receipt.json",
    "docs/reference/2026-04-28-market-skill-baseline.json",
)


@pytest.mark.parametrize("relative_path", REMOVED_DOCS_FILES)
def test_removed_docs_file_is_absent(relative_path: str) -> None:
    assert not (REPO_ROOT / relative_path).exists()


@pytest.mark.parametrize("relative_path", KEPT_BECAUSE_READ)
def test_file_a_test_or_workflow_reads_is_kept(relative_path: str) -> None:
    assert (REPO_ROOT / relative_path).is_file()


def test_docs_tree_has_no_empty_directory() -> None:
    empty = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "docs").rglob("*")
        if path.is_dir() and not any(path.iterdir())
    ]
    assert empty == []
