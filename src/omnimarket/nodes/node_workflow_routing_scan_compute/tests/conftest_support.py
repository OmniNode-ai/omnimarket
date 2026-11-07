# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the workflow routing scan tests (OMN-22948): fixtures only, no node logic."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
    ModelRoutingRoute,
    ModelRoutingScanPolicy,
    ModelScannedRepository,
    ModelWorkflowFile,
    ModelWorkflowRoutingScanRequest,
)

NAME = "node_workflow_routing_scan_compute"

HOSTED_WORKFLOW = """\
name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo ok
"""

PLANTED_WORKFLOW = """\
name: planted
on: [push]
jobs:
  lab-only:
    runs-on: [self-hosted, linux]
    steps:
      - run: echo lab
"""


EXPECTED = tuple(f"repo_{index:02d}" for index in range(1, 25))


def overlay() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("overlay.yaml").read_text()
    )
    return loaded


def policy() -> ModelRoutingScanPolicy:
    return ModelRoutingScanPolicy.model_validate(overlay())


def repository(
    name: str,
    *workflows: tuple[str, str],
    visibility: str = "private",
) -> ModelScannedRepository:
    return ModelScannedRepository(
        name=name,
        visibility=visibility,
        workflows=tuple(
            ModelWorkflowFile(path=f".github/workflows/{file}", source=source)
            for file, source in workflows
        ),
    )


def request(
    repositories: tuple[ModelScannedRepository, ...],
    routes: tuple[ModelRoutingRoute, ...] = (),
    variables: tuple[Any, ...] = (),
) -> ModelWorkflowRoutingScanRequest:
    return ModelWorkflowRoutingScanRequest(
        policy=policy(),
        expected_repositories=EXPECTED,
        repositories=repositories,
        routes=routes,
        variables=variables,
    )


def all_hosted_repositories() -> tuple[ModelScannedRepository, ...]:
    return tuple(repository(name, ("ci.yml", HOSTED_WORKFLOW)) for name in EXPECTED)
