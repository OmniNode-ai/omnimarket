# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""omnimarket's CI no longer checks out onex_change_control (OMN-20074).

The contract-topic-graph job used to check out the governance repo only to read
its contract.yaml files for census completeness. omnimarket is cut over to its
own contracts, so that checkout, and the graph's reference to the package, are
gone. Five occ-* workflows (the S7 replay and the emitter golden gate among
them) only operated on companions that nothing in this repository mints any more
and are deleted. The companion scripts with no caller left in this repository,
and the lists that name the governance repository as a sibling to protect, scan
or import, are gone too.

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
# and the backfill writes new OCC contracts on a schedule. The S7 replay and the
# emitter golden gate went once every remaining caller of the companion
# publisher read it at an immutable ref (the omniclaude reusables default to a
# release tag, and the one other caller pins those reusables by commit).
DELETED = (
    "occ-conflicted-companion-remint.yml",
    "occ-behavior-proof-backfill.yml",
    "occ-n10-streak-report.yml",
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


# Companion scripts that no workflow, hook, node or bound contract in this
# repository calls. occ_receipt_runner.py stays: contract OMN-19267 (AC5) binds
# a test of it.
DELETED_SCRIPTS = (
    "scripts/ci/occ_window_quiet_wait.py",
    "scripts/ci/build_occ_observation_effect_payload.py",
    "scripts/ci/check_occ_companion_merged.py",
    "scripts/ci/occ_companion_merge_heal.py",
    "scripts/ci/check_minted_evidence_is_probative.py",
    "scripts/ci/check_self_bind_entry_binding.py",
    "scripts/publish_occ_companion_effect_command.py",
)


@pytest.mark.parametrize("relative", DELETED_SCRIPTS)
def test_script_without_a_caller_is_deleted(relative: str) -> None:
    assert not (ROOT / relative).exists()


def test_script_deletion_positive_control() -> None:
    """The same read finds a companion script that is still there, so the
    absences above are not an unreadable tree."""
    assert (ROOT / "scripts/ci/occ_receipt_runner.py").is_file()


# Files that listed the governance repository as a repository to protect, scan or
# import. Each is read for the bare name, so a prose mention would also fail it.
NAMES_NO_CHANGE_CONTROL = (
    ".github/required-checks.yaml",
    "scripts/apply_branch_protection.sh",
    "scripts/validation/run_leaked_literals_org_wide.sh",
    "scripts/ci/check_node_metadata_dependencies.py",
    "scripts/ci/check_workflow_module_refs.py",
)


@pytest.mark.parametrize("relative", NAMES_NO_CHANGE_CONTROL)
def test_file_names_no_change_control(relative: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")
    assert "onex_change_control" not in text
    assert "onex-change-control" not in text


def test_names_no_change_control_positive_control() -> None:
    """The same read finds a name that is there, so the absences above are not
    an unreadable or empty file."""
    text = (ROOT / "scripts/apply_branch_protection.sh").read_text(encoding="utf-8")
    assert "omnibase_core" in text


def test_imperative_contract_guard_required_check_names_its_core_owner() -> None:
    parsed = yaml.safe_load((ROOT / ".github/required-checks.yaml").read_text())
    rows = [
        row
        for row in parsed["gates"]
        if row.get("caller_workflow") == "imperative-contract-guard.yml"
    ]
    assert len(rows) == 1
    assert rows[0]["cross_repo_ref"].startswith(
        "OmniNode-ai/omnibase_core/.github/workflows/imperative-contract-guard.yml@"
    )
