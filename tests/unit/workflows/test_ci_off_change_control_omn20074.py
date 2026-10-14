# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket's CI no longer checks out onex_change_control (OMN-20074).

The contract-topic-graph job used to check out the governance repo only to read
its contract.yaml files for census completeness. omnimarket is cut over to its
own contracts, so that checkout, and the graph's reference to the package, are
gone. Three occ-* workflows only operated on companions that nothing in this
repository mints any more and are deleted; two stay until the retirement plan's
S9 (see KEPT_UNTIL_S9).

These tests read this repository's own files, so they run on every runner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.validators.contract_topic_graph import (
    CHECKOUT_PACKAGES,
    GRAPH_PACKAGES,
    INSTALLED_PACKAGES,
)

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
CHANGE_CONTROL_REPOSITORY = "OmniNode-ai/onex_change_control"
pytestmark = pytest.mark.unit

# Dead: the autobind caller and occ-receipt-runner.yml they act through are gone
# from this repository, the N=10 auto-author window has no companions to count,
# and the backfill writes new OCC contracts on a schedule.
DELETED = (
    "occ-conflicted-companion-remint.yml",
    "occ-behavior-proof-backfill.yml",
    "occ-n10-streak-report.yml",
)
# Stay until S9: the S7 replay is still dispatched for the repositories that have
# not cut over, and the emitter golden gate guards the companion emitter node
# that still serves them.
KEPT_UNTIL_S9 = (
    "occ-s7-replay.yml",
    "occ-emitter-golden-gate.yml",
)


def _checkout_repositories(workflow: Path) -> list[str]:
    parsed: dict[str, Any] = yaml.safe_load(workflow.read_text())
    repositories: list[str] = []
    for job in parsed["jobs"].values():
        for step in job.get("steps") or []:
            repository = (step.get("with") or {}).get("repository")
            if repository:
                repositories.append(str(repository))
    return repositories


def test_ci_workflow_checks_out_no_change_control() -> None:
    repositories = _checkout_repositories(WORKFLOWS / "ci.yml")
    assert CHANGE_CONTROL_REPOSITORY not in repositories


def test_ci_workflow_positive_control_sees_other_checkouts() -> None:
    """The reader finds the census-only checkouts that remain, so the absence
    above is a reading of the file and not an empty parse."""
    repositories = _checkout_repositories(WORKFLOWS / "ci.yml")
    assert "OmniNode-ai/omniclaude" in repositories
    assert "OmniNode-ai/omniintelligence" in repositories


def test_contract_topic_graph_does_not_expect_change_control() -> None:
    assert "onex_change_control" not in GRAPH_PACKAGES
    assert "onex_change_control" not in CHECKOUT_PACKAGES
    assert "onex_change_control" not in INSTALLED_PACKAGES


@pytest.mark.parametrize("name", DELETED)
def test_dead_occ_workflow_is_deleted(name: str) -> None:
    assert not (WORKFLOWS / name).exists()


@pytest.mark.parametrize("name", KEPT_UNTIL_S9)
def test_occ_workflow_kept_until_s9_still_present(name: str) -> None:
    assert (WORKFLOWS / name).is_file()
