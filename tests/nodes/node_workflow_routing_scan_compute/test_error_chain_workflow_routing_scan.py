# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each way a definition stays unresolved is typed, counted and named (OMN-22948)."""

from __future__ import annotations

from omnimarket.nodes.node_workflow_routing_scan_compute.handlers import (
    HandlerWorkflowRoutingScan,
)
from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
    EnumRoutingResolution,
    EnumRoutingSelection,
    ModelRepositoryVariable,
)
from tests.nodes.node_workflow_routing_scan_compute.support import (
    EXPECTED,
    HOSTED_WORKFLOW,
    all_hosted_repositories,
    job_workflow,
    repository,
    request,
    with_workflow,
)

_HANDLER = HandlerWorkflowRoutingScan()


def test_error_an_expected_repository_never_read_is_unresolved_and_named() -> None:
    result = _HANDLER.handle(request(all_hosted_repositories()[:-1]))
    missing = EXPECTED[-1]

    assert result.complete is False
    assert result.repositories_unread == (missing,)
    assert result.unresolved_count == 1
    unread = [s for s in result.scenarios if s.repository == missing]
    assert [s.selection for s in unread] == [EnumRoutingSelection.UNREAD_REPOSITORY]


def test_error_a_workflow_that_does_not_parse_is_unresolved() -> None:
    result = with_workflow("jobs: [unclosed: {")

    assert result.unresolved_count == 1
    assert result.scenarios[0].selection is EnumRoutingSelection.UNPARSEABLE


def test_error_a_seam_variable_with_a_non_hosted_live_value_stays_unresolved() -> None:
    names = EXPECTED
    workflow = job_workflow("${{ fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON) }}")
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
