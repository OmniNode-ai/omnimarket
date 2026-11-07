# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each way a definition stays unresolved is typed, counted and named (OMN-22948)."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_workflow_routing_scan_compute.handlers import (
    HandlerWorkflowRoutingScan,
)
from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
    EnumRoutingResolution,
    EnumRoutingSelection,
    ModelRepositoryVariable,
)
from omnimarket.nodes.node_workflow_routing_scan_compute.tests.conftest_support import (
    EXPECTED,
    HOSTED_WORKFLOW,
    PLANTED_WORKFLOW,
    all_hosted_repositories,
    policy,
    repository,
    request,
)

_HANDLER = HandlerWorkflowRoutingScan()


def _job(runs_on: str) -> str:
    return f"name: x\non: [push]\njobs:\n  j:\n    runs-on: {runs_on}\n    steps: []\n"


def _with(workflow: str) -> Any:
    names = EXPECTED
    repositories = (
        repository(names[0], ("x.yml", workflow)),
        *all_hosted_repositories()[1:],
    )
    return _HANDLER.handle(request(repositories))


def test_error_an_expected_repository_never_read_is_unresolved_and_named() -> None:
    result = _HANDLER.handle(request(all_hosted_repositories()[:-1]))
    missing = EXPECTED[-1]

    assert result.complete is False
    assert result.repositories_unread == (missing,)
    assert result.unresolved_count == 1
    unread = [s for s in result.scenarios if s.repository == missing]
    assert [s.selection for s in unread] == [EnumRoutingSelection.UNREAD_REPOSITORY]


def test_error_a_workflow_that_does_not_parse_is_unresolved() -> None:
    result = _with("jobs: [unclosed: {")

    assert result.unresolved_count == 1
    assert result.scenarios[0].selection is EnumRoutingSelection.UNPARSEABLE


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
    result = _with(_job(runs_on))

    assert result.unresolved_count >= 1
    assert selection in {s.selection for s in result.scenarios}


def test_error_unresolved_zero_planted_job_makes_the_count_non_zero() -> None:
    clean = _HANDLER.handle(request(all_hosted_repositories()))
    planted = _with(PLANTED_WORKFLOW)

    assert clean.unresolved_count == 0
    assert planted.unresolved_count > 0


def test_error_unresolved_zero_reading_fails_under_the_injected_hosted_pattern_break() -> (
    None
):
    """Named break: a hosted pattern that matches everything hides the planted job; the reading must notice."""
    broken = policy().model_copy(update={"hosted_label_pattern": ".*"})
    names = EXPECTED
    repositories = (
        repository(names[0], ("x.yml", _job("self-hosted"))),
        *all_hosted_repositories()[1:],
    )
    honest = request(repositories)
    sabotaged = honest.model_copy(update={"policy": broken})

    assert _HANDLER.handle(honest).unresolved_count > 0
    # the sabotaged scan reads zero, and the control below is what flags it
    assert _HANDLER.handle(sabotaged).unresolved_count == 0
    with pytest.raises(AssertionError):
        assert _HANDLER.handle(sabotaged).unresolved_count > 0


def test_error_a_seam_variable_with_a_non_hosted_live_value_stays_unresolved() -> None:
    names = EXPECTED
    workflow = _job("${{ fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON) }}")
    repositories = (
        repository(names[0], ("x.yml", workflow)),
        *all_hosted_repositories()[1:],
    )
    lab = ModelRepositoryVariable(
        repository=names[0],
        name="OMNI_TRUSTED_CI_RUNS_ON_JSON",
        value='["self-hosted"]',
    )
    hosted = ModelRepositoryVariable(
        repository=names[0],
        name="OMNI_TRUSTED_CI_RUNS_ON_JSON",
        value='["ubuntu-latest"]',
    )

    assert (
        _HANDLER.handle(request(repositories, variables=(lab,))).unresolved_count == 1
    )
    ok = _HANDLER.handle(request(repositories, variables=(hosted,)))
    assert ok.unresolved_count == 0
    assert {s.resolution for s in ok.scenarios} == {EnumRoutingResolution.HOSTED}


def test_error_a_stale_route_is_a_finding_and_resolves_nothing() -> None:
    from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
        ModelRoutingRoute,
    )

    names = EXPECTED
    route = ModelRoutingRoute(
        repository=names[0],
        workflow=".github/workflows/gone.yml",
        job="vanished",
        kind="retires_with_runners",
    )
    result = _HANDLER.handle(request(all_hosted_repositories(), routes=(route,)))

    assert result.unresolved_count == 0
    assert [f.job for f in result.stale_routes] == ["vanished"]


def test_error_a_repository_outside_the_expected_set_is_reported_not_dropped() -> None:
    extra = repository("some_other_repo", ("ci.yml", HOSTED_WORKFLOW))
    result = _HANDLER.handle(request((*all_hosted_repositories(), extra)))

    assert result.repositories_extra == ("some_other_repo",)
    assert result.unresolved_count == 0
