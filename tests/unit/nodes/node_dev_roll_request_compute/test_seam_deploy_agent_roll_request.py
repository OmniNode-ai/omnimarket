# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the roll request the node would make against the deploy agent's roll request contract.

The agent's command model, ``ModelRebuildRequested`` (omnibase_infra, scripts/deploy-agent/
deploy_agent/events.py), cannot be imported here and a cross-repository check that skips when
the other clone is absent is refused by the skip ratchet. So the agent's wire fields are spelled
below, read from omnibase_infra dev at 389b31d66d08. This test goes red when OUR request model
changes shape; when the agent's model changes, the spelled contract is updated in the same
change that follows it, and the omnibase_infra side is read on the lab (the PR body names the
reading) by running the same field comparison against the agent's own model.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.events.dev_roll import (
    ModelDevRollDecided,
    ModelDevRollRequest,
    ModelDevTipChanged,
)
from omnimarket.nodes.node_dev_roll_request_compute.handlers import decide_dev_roll
from omnimarket.nodes.node_dev_roll_request_compute.models import (
    ModelDevRollPolicy,
)
from tests.unit.nodes.node_dev_roll_request_compute.conftest import facts, tip

pytestmark = pytest.mark.unit

AGENT_ROLL_REQUEST_CONTRACT: dict[str, Any] = {
    "read_at": "omnibase_infra dev 389b31d66d08",
    "model": "ModelRebuildRequested (extra fields forbidden)",
    "required": {"correlation_id", "requested_by", "scope", "runtime_lane"},
    "optional": {
        "build_source",
        "services",
        "git_ref",
        "image_ref",
        "image_digest",
        "rollback",
        "open_pr",
        "requested_at",
    },
    "scope": {"full", "runtime", "core"},
    "runtime_lane": {"dev"},  # the only lane this node names; the agent accepts others
}
OUR_REQUEST_FIELDS = (
    "correlation_id",
    "git_ref",
    "requested_at",
    "requested_by",
    "runtime_lane",
    "scope",
)
TIP_EVENT_FIELDS = ("event_id", "merged_at", "repository", "sha")


def test_our_request_has_no_field_the_agent_forbids() -> None:
    allowed = (
        AGENT_ROLL_REQUEST_CONTRACT["required"]
        | AGENT_ROLL_REQUEST_CONTRACT["optional"]
    )
    assert set(ModelDevRollRequest.model_fields) <= allowed


def test_our_request_carries_every_field_the_agent_requires() -> None:
    required = AGENT_ROLL_REQUEST_CONTRACT["required"]
    assert required <= set(ModelDevRollRequest.model_fields)


def test_a_roll_decision_payload_stays_inside_the_agents_enums(
    policy: ModelDevRollPolicy,
) -> None:
    d = decide_dev_roll(facts((tip("omnibase_infra", age_seconds=3000),)), policy)
    assert isinstance(d, ModelDevRollDecided)
    assert d.would_request is not None
    payload = d.would_request.wire_payload()
    assert payload["scope"] in AGENT_ROLL_REQUEST_CONTRACT["scope"]
    assert payload["runtime_lane"] in AGENT_ROLL_REQUEST_CONTRACT["runtime_lane"]
    assert set(payload) == set(OUR_REQUEST_FIELDS)
    assert isinstance(payload["git_ref"], str)
    assert len(payload["git_ref"]) == 40
    assert str(payload["requested_at"]).endswith(("Z", "+00:00"))


def test_our_side_changing_shape_is_red_the_request_fields_are_pinned() -> None:
    assert tuple(sorted(ModelDevRollRequest.model_fields)) == OUR_REQUEST_FIELDS


def test_the_tip_event_fields_are_pinned_for_the_ci_publisher() -> None:
    assert tuple(sorted(ModelDevTipChanged.model_fields)) == TIP_EVENT_FIELDS
    assert set(ModelDevTipChanged.model_json_schema()["required"]) == set(
        TIP_EVENT_FIELDS
    )


def test_failing_control_a_renamed_field_would_leave_the_agents_required_set() -> None:
    renamed = (set(ModelDevRollRequest.model_fields) - {"git_ref"}) | {"ref"}
    allowed = (
        AGENT_ROLL_REQUEST_CONTRACT["required"]
        | AGENT_ROLL_REQUEST_CONTRACT["optional"]
    )
    assert not renamed <= allowed, (
        "an invented field name is outside the agent's contract"
    )
