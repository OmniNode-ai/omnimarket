# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-22948: a scan reads every workflow definition of all 24 repositories and emits each scenario's resolution."""

from __future__ import annotations

from omnimarket.nodes.node_workflow_routing_scan_compute.handlers import (
    HandlerWorkflowRoutingScan,
)
from omnimarket.nodes.node_workflow_routing_scan_compute.tests.conftest_support import (
    EXPECTED,
    HOSTED_WORKFLOW,
    PLANTED_WORKFLOW,
    all_hosted_repositories,
    repository,
    request,
)

_HANDLER = HandlerWorkflowRoutingScan()


def test_all_repositories_every_definition_of_every_repository_emits_a_scenario() -> (
    None
):
    names = EXPECTED
    repositories = tuple(
        repository(
            name,
            ("ci.yml", HOSTED_WORKFLOW),
            ("lab.yml", PLANTED_WORKFLOW),
            ("matrix.yml", _matrix("ubuntu-latest", "self-hosted")),
        )
        for name in names
    )

    result = _HANDLER.handle(request(repositories))

    assert result.repositories_read == len(names) == 24
    assert result.workflows_read == 24 * 3
    assert {s.repository for s in result.scenarios} == set(names)
    for name in names:
        files = {s.workflow for s in result.scenarios if s.repository == name}
        assert files == {
            ".github/workflows/ci.yml",
            ".github/workflows/lab.yml",
            ".github/workflows/matrix.yml",
        }
    assert all(s.resolution is not None for s in result.scenarios)
    assert result.scenario_count == len(result.scenarios)


def test_all_repositories_a_repository_not_read_is_never_silently_complete() -> None:
    names = EXPECTED
    result = _HANDLER.handle(request(all_hosted_repositories()[:23]))

    assert result.repositories_read == 23
    assert result.complete is False
    assert result.repositories_unread == (names[23],)


def _matrix(*labels: str) -> str:
    items = ", ".join(labels)
    return (
        "name: m\non: [push]\njobs:\n  j:\n    strategy:\n      matrix:\n"
        f"        os: [{items}]\n    runs-on: ${{{{ matrix.os }}}}\n    steps: []\n"
    )
