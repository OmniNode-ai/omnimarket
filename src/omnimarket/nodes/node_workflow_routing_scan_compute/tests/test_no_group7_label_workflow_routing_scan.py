# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-22949: no workflow of a public repository names a group-7 label; a planted one makes the count non-zero."""

from __future__ import annotations

from omnimarket.nodes.node_workflow_routing_scan_compute.handlers import (
    HandlerWorkflowRoutingScan,
)
from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
    ModelRepositoryVariable,
    ModelScannedRepository,
    ModelWorkflowRoutingScanResult,
)
from tests.nodes.node_workflow_routing_scan_compute.support import (
    EXPECTED,
    all_hosted_repositories,
    job_workflow,
    repository,
    request,
)

_HANDLER = HandlerWorkflowRoutingScan()
# Synthetic: the real group-7 labels live in the caller's private overlay home, never in this repository.
_G7 = ("example-g7-runner", "example-g7-large")


def _scan(
    repositories: tuple[ModelScannedRepository, ...],
    labels: tuple[str, ...] = _G7,
    variables: tuple[ModelRepositoryVariable, ...] = (),
) -> ModelWorkflowRoutingScanResult:
    asked = request(repositories, variables=variables).model_copy(
        update={"group7_labels": labels}
    )
    return _HANDLER.handle(asked)


def _with_first(
    first: ModelScannedRepository, *rest: ModelScannedRepository
) -> tuple[ModelScannedRepository, ...]:
    return (first, *rest, *all_hosted_repositories()[1 + len(rest) :])


def _public(*workflows: tuple[str, str]) -> ModelScannedRepository:
    return repository(EXPECTED[0], *workflows, visibility="public")


def test_no_group7_label_clean_public_repositories_read_zero_with_labels_declared() -> (
    None
):
    repos = _with_first(_public(("ci.yml", job_workflow("ubuntu-latest"))))

    result = _scan(repos)

    assert result.group7_labels_declared == len(_G7)
    assert result.group7_public_count == 0
    assert result.group7_public == ()


def test_no_group7_label_planted_job_in_a_public_repository_makes_the_count_non_zero() -> (
    None
):
    repos = _with_first(
        _public(("x.yml", job_workflow("[self-hosted, example-g7-runner]")))
    )

    result = _scan(repos)

    assert result.group7_public_count == 1
    naming = result.group7_public[0]
    assert (naming.repository, naming.job, naming.label) == (
        EXPECTED[0],
        "j",
        "example-g7-runner",
    )


def test_no_group7_label_the_same_job_in_a_private_repository_is_not_counted() -> None:
    private = repository(
        EXPECTED[0],
        ("x.yml", job_workflow("[self-hosted, example-g7-runner]")),
        visibility="private",
    )

    result = _scan(_with_first(private))

    assert result.group7_public_count == 0


def test_no_group7_label_label_match_ignores_case() -> None:
    repos = _with_first(_public(("x.yml", job_workflow("[Example-G7-Large]"))))

    assert _scan(repos).group7_public_count == 1


def test_no_group7_label_no_labels_declared_is_not_a_zero_reading() -> None:
    repos = _with_first(_public(("x.yml", job_workflow("[example-g7-runner]"))))

    result = _scan(repos, labels=())

    assert result.group7_labels_declared == 0
    assert result.group7_public_count == 0


def test_no_group7_label_a_seam_variable_value_naming_the_label_is_counted() -> None:
    workflow = job_workflow("${{ fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON) }}")
    repos = _with_first(_public(("x.yml", workflow)))
    variables = (
        ModelRepositoryVariable(
            repository=EXPECTED[0],
            name="OMNI_TRUSTED_CI_RUNS_ON_JSON",
            value='["example-g7-runner"]',
        ),
    )

    assert _scan(repos, variables=variables).group7_public_count == 1


def test_no_group7_label_a_public_caller_of_a_private_callee_naming_it_is_counted() -> (
    None
):
    caller = (
        "name: c\non: [pull_request]\njobs:\n  call:\n"
        "    uses: example/repo_02/.github/workflows/callee.yml@main\n"
    )
    callee = job_workflow("[example-g7-runner]")
    public = _public(("caller.yml", caller))
    private = repository(EXPECTED[1], ("callee.yml", callee), visibility="private")

    result = _scan(_with_first(public, private))

    assert result.group7_public_count == 1
    assert result.group7_public[0].job == "call"
    assert result.group7_public[0].label == "example-g7-runner"
