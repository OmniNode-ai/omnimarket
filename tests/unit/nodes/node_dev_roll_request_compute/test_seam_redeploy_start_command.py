# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the roll request the batching node would make against the model the CI job publishes today.

``ModelRedeployStartCommand`` (node_redeploy_orchestrator) is the command the merged-change
CI job sends before the deploy agent's own command is built from it. This repository imports it,
so the check runs everywhere: if either model changes a field this test goes red.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.nodes.node_dev_roll_request_compute.models import ModelDevRollRequest
from omnimarket.nodes.node_redeploy_orchestrator.models.model_redeploy_start_command import (
    ModelRedeployStartCommand,
)

pytestmark = pytest.mark.unit


def _request(git_ref: str = "a" * 40) -> ModelDevRollRequest:
    return ModelDevRollRequest(
        correlation_id=uuid4(),
        requested_by="seam-test",
        scope="full",
        runtime_lane="dev",
        git_ref=git_ref,
        requested_at=datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
    )


def test_every_field_of_the_roll_request_is_a_field_of_the_start_command() -> None:
    assert set(ModelDevRollRequest.model_fields) <= set(
        ModelRedeployStartCommand.model_fields
    )


def test_the_roll_request_payload_validates_as_the_start_command() -> None:
    payload = _request().wire_payload()
    command = ModelRedeployStartCommand.model_validate(payload)
    assert command.git_ref == "a" * 40
    assert command.requested_by == "seam-test"
    assert command.scope.value == "full"
    assert command.runtime_lane.value == "dev"


def test_a_branch_alias_ref_also_validates_as_the_fallback() -> None:
    command = ModelRedeployStartCommand.model_validate(
        _request("origin/dev").wire_payload()
    )
    assert command.git_ref == "origin/dev"


def test_failing_control_a_field_the_start_command_does_not_have_is_refused() -> None:
    payload = {**_request().wire_payload(), "batch_id": "x"}
    with pytest.raises(ValueError, match="batch_id"):
        ModelRedeployStartCommand.model_validate(payload)
