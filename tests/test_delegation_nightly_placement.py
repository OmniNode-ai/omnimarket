# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation nightly must stay pinned to a lab-reachable runner (OMN-18349).

The workflow probes the lab lane over the LAN. It is correct only on a runner
inside that network, so its labels are a literal in the workflow file and not a
read of a shared runner-routing variable. This module is the mechanical half of
that: re-introducing the variable read is a red test, not a review catch.

Every assertion below carries its own falsification control -- a synthetic
``runs-on`` known to be wrong -- because a checker that cannot be shown to fail
is not evidence that the thing it checks is true.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scripts.ci.check_lane_job_placement import (
    LANE_BOUND_JOBS,
    check_repo,
    check_runs_on,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NIGHTLY = REPO_ROOT / ".github/workflows/delegation-regression-nightly.yml"


@pytest.mark.unit
class TestLaneBoundJobPlacement:
    """The nightly pins literal labels, and the checker that says so can fail."""

    def test_repo_has_no_placement_violations(self) -> None:
        assert check_repo(REPO_ROOT) == []

    def test_nightly_pins_the_literal_lab_labels(self) -> None:
        document = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))
        runs_on = document["jobs"]["golden-tasks"]["runs-on"]
        assert runs_on == ["self-hosted", "omnibase-ci"], runs_on

    def test_checker_rejects_a_routing_variable_read(self) -> None:
        """The exact regression: placement delegated to a shared variable.

        This is the value the workflow carried until OMN-18349, and the value a
        future routing change would most plausibly restore.
        """
        expression = (
            "${{ fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON || "
            '\'["self-hosted","omnibase-ci"]\') }}'
        )
        problems = check_runs_on(expression, ["self-hosted", "omnibase-ci"])
        assert problems, "an expression-driven runs-on must be refused"
        assert "expression" in problems[0]

    def test_checker_rejects_a_hosted_label(self) -> None:
        problems = check_runs_on("ubuntu-latest", ["self-hosted", "omnibase-ci"])
        assert problems, "a hosted label must be refused for a lane-bound job"

    def test_checker_rejects_an_expression_inside_a_list(self) -> None:
        problems = check_runs_on(
            ["self-hosted", "${{ vars.SOMETHING }}"], ["self-hosted", "omnibase-ci"]
        )
        assert problems, "an expression anywhere in the list must be refused"

    def test_checker_accepts_only_the_declared_labels(self) -> None:
        assert (
            check_runs_on(
                ["self-hosted", "omnibase-ci"], ["self-hosted", "omnibase-ci"]
            )
            == []
        )
        assert check_runs_on(
            ["omnibase-ci", "self-hosted"], ["self-hosted", "omnibase-ci"]
        )

    def test_every_declared_lane_bound_workflow_exists(self) -> None:
        """A stale row would make check_repo vacuous for that workflow."""
        for workflow_rel in LANE_BOUND_JOBS:
            assert (REPO_ROOT / workflow_rel).exists(), workflow_rel


@pytest.mark.unit
class TestNightlyLaneWiring:
    """The workflow stops carrying wiring that silently resolved to nothing."""

    def test_no_broker_address_literal_in_the_workflow(self) -> None:
        """The bus address is resolved from config/ci_bus_lanes.yaml, not here."""
        text = NIGHTLY.read_text(encoding="utf-8")
        job = yaml.safe_load(text)["jobs"]["golden-tasks"]
        assert "ONEX_E2E_KAFKA_BOOTSTRAP" not in job.get("env", {})

    def test_sasl_principal_is_wired_for_the_declared_transport(self) -> None:
        """The dev lane declares SASL; a job that omits the principal hangs."""
        job = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"][
            "golden-tasks"
        ]
        env = job.get("env", {})
        assert "KAFKA_SASL_USERNAME" in env
        assert "KAFKA_SASL_" + "PASSWORD" in env

    def test_default_lane_is_the_declared_proof_lane(self) -> None:
        job = yaml.safe_load(NIGHTLY.read_text(encoding="utf-8"))["jobs"][
            "golden-tasks"
        ]
        assert "'stability-test'" in job["env"]["ONEX_E2E_LANE"]
