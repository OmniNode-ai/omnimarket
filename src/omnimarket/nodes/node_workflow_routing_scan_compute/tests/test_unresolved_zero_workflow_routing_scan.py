# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 of OMN-22948: the unresolved count is zero only when nothing selects a lab label unresolved; a planted job makes it non-zero."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_workflow_routing_scan_compute.handlers import (
    HandlerWorkflowRoutingScan,
)
from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
    EnumRoutingSelection,
)
from tests.nodes.node_workflow_routing_scan_compute.support import (
    EXPECTED,
    PLANTED_WORKFLOW,
    all_hosted_repositories,
    job_workflow,
    policy,
    repository,
    request,
    with_workflow,
)

_HANDLER = HandlerWorkflowRoutingScan()


@pytest.mark.parametrize(
    ("runs_on", "selection"),
    [
        ("self-hosted", EnumRoutingSelection.LAB_LABEL),
        ("[self-hosted, linux]", EnumRoutingSelection.LAB_LABEL),
        ("omni-lab-runner", EnumRoutingSelection.LAB_LABEL),
        ("{ group: omnibase-ci }", EnumRoutingSelection.RUNNER_GROUP),
        ("${{ needs.pick.outputs.runner }}", EnumRoutingSelection.COMPUTED),
        ("${{ vars.SOME_RUNNER }}", EnumRoutingSelection.UNDECLARED_VARIABLE),
        (
            "${{ fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON) }}",
            EnumRoutingSelection.UNREAD_SEAM_VARIABLE,
        ),
    ],
)
def test_error_unresolved_zero_is_refused_for_each_lab_selection(
    runs_on: str, selection: EnumRoutingSelection
) -> None:
    result = with_workflow(job_workflow(runs_on))

    assert result.unresolved_count >= 1
    assert selection in {s.selection for s in result.scenarios}


def test_error_unresolved_zero_planted_job_makes_the_count_non_zero() -> None:
    clean = _HANDLER.handle(request(all_hosted_repositories()))
    planted = with_workflow(PLANTED_WORKFLOW)

    assert clean.unresolved_count == 0
    assert planted.unresolved_count > 0


def test_error_unresolved_zero_reading_fails_under_the_injected_hosted_pattern_break() -> (
    None
):
    """Named break: a hosted pattern that matches everything hides the planted job; the reading must notice."""
    broken = policy().model_copy(update={"hosted_label_pattern": ".*"})
    names = EXPECTED
    repositories = (
        repository(names[0], ("x.yml", job_workflow("self-hosted"))),
        *all_hosted_repositories()[1:],
    )
    honest = request(repositories)
    sabotaged = honest.model_copy(update={"policy": broken})

    assert _HANDLER.handle(honest).unresolved_count > 0
    # the sabotaged scan reads zero, and the control below is what flags it
    assert _HANDLER.handle(sabotaged).unresolved_count == 0
    with pytest.raises(AssertionError):
        assert _HANDLER.handle(sabotaged).unresolved_count > 0
