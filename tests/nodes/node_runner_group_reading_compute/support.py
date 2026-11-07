# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the runner-group reading tests (OMN-22947): fixtures only, no node logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_runner_group_reading_compute.models import (
    EnumGroupVisibility,
    EnumRepositoryVisibility,
    ModelCanaryFacts,
    ModelCanaryRun,
    ModelGroupFact,
    ModelGroupRunner,
    ModelRepositoryFact,
    ModelRunnerGroupReadingRequest,
    ModelRunnerGroupReadingSettings,
)

NAME = "node_runner_group_reading_compute"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
LAB_GROUP_ID = 3
CLOSED_GROUP_ID = 4
PRIVATE_GROUP_ID = 7


def overlay() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("overlay.yaml").read_text()
    )
    return loaded


def settings() -> ModelRunnerGroupReadingSettings:
    return ModelRunnerGroupReadingSettings.model_validate(overlay())


def lab_runners(count: int = 2) -> tuple[ModelGroupRunner, ...]:
    return tuple(
        ModelGroupRunner(name=f"lab-runner-{i}", labels=("self-hosted", "linux"))
        for i in range(count)
    )


def hosted_runners() -> tuple[ModelGroupRunner, ...]:
    return (ModelGroupRunner(name="hosted-runner-1", labels=("ubuntu-latest",)),)


def lab_group(
    *,
    group_id: int = LAB_GROUP_ID,
    allows_public: bool = False,
    visibility: EnumGroupVisibility = EnumGroupVisibility.ALL,
    selected: tuple[str, ...] = (),
) -> ModelGroupFact:
    return ModelGroupFact(
        group_id=group_id,
        name=f"group-{group_id}",
        visibility=visibility,
        allows_public_repositories=allows_public,
        selected_repositories=selected,
        runners=lab_runners(),
    )


def hosted_group(*, group_id: int = 9, allows_public: bool = True) -> ModelGroupFact:
    return ModelGroupFact(
        group_id=group_id,
        name=f"group-{group_id}",
        visibility=EnumGroupVisibility.ALL,
        allows_public_repositories=allows_public,
        runners=hosted_runners(),
    )


def repositories() -> tuple[ModelRepositoryFact, ...]:
    return (
        ModelRepositoryFact(
            name="example-public", visibility=EnumRepositoryVisibility.PUBLIC
        ),
        ModelRepositoryFact(
            name="example-private", visibility=EnumRepositoryVisibility.PRIVATE
        ),
    )


def canary(*runs: ModelCanaryRun, age_seconds: int = 60) -> ModelCanaryFacts:
    return ModelCanaryFacts(read_at=NOW - timedelta(seconds=age_seconds), runs=runs)


def closed_groups() -> tuple[ModelGroupFact, ...]:
    """The shape the plan wants: every group that holds lab runners is closed to public repositories."""
    return (
        lab_group(group_id=LAB_GROUP_ID, allows_public=False),
        lab_group(group_id=CLOSED_GROUP_ID, allows_public=False),
        lab_group(
            group_id=PRIVATE_GROUP_ID,
            allows_public=False,
            visibility=EnumGroupVisibility.SELECTED,
            selected=("example-private",),
        ),
        hosted_group(),
    )


def request(
    groups: tuple[ModelGroupFact, ...] | None = None,
    *,
    canary_facts: ModelCanaryFacts | None = None,
    age_seconds: int = 120,
    read_failure: Any = None,
) -> ModelRunnerGroupReadingRequest:
    return ModelRunnerGroupReadingRequest(
        correlation_id="rgr-1",
        now=NOW,
        read_at=NOW - timedelta(seconds=age_seconds),
        settings=settings(),
        groups=closed_groups() if groups is None else groups,
        repositories=repositories(),
        canary=canary() if canary_facts is None else canary_facts,
        read_failure=read_failure,
    )
