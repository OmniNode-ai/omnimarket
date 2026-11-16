# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the unpopulated local-share alarm out of the health contract."""

from __future__ import annotations

import shlex

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_health_compute.handlers.handler_delegation_health import (
    HandlerDelegationHealth,
    load_rules,
)
from omnimarket.nodes.node_delegation_health_compute.models.model_delegation_health import (
    EnumDelegationHealthFindingKind,
    ModelDelegationHealthRequest,
    ModelDelegationHealthRules,
)

pytestmark = pytest.mark.unit


def test_existing_caller_does_not_gain_an_unpopulated_local_share_warning() -> None:
    verdict = HandlerDelegationHealth().handle(
        ModelDelegationHealthRequest(
            correlation_id="health", quiet_minutes=360, total_rows=5
        )
    )
    assert not any("local share" in line for line in verdict.not_judged)


def test_existing_rule_payload_needs_no_local_share_fields() -> None:
    payload = load_rules().model_dump(mode="json")
    payload.pop("local_share_window_minutes", None)
    payload.pop("local_share_min_runs", None)
    payload["fixes"].pop("local_share_zero", None)
    ModelDelegationHealthRules.model_validate(payload)


def test_unimplemented_local_share_finding_is_not_advertised() -> None:
    assert "local_share_zero" not in {
        kind.value for kind in EnumDelegationHealthFindingKind
    }


@pytest.mark.parametrize("runs", [4, 5, 40])
def test_unread_local_share_counts_are_not_accepted_as_health_evidence(
    runs: int,
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelDelegationHealthRequest.model_validate(
            {
                "correlation_id": "health",
                "quiet_minutes": 360,
                "total_rows": 100,
                "local_share": {
                    "window_minutes": 360,
                    "delegated_runs": runs,
                    "local_runs": 0,
                    "local_endpoints_up": ["a point-in-time endpoint probe"],
                },
            }
        )


def test_remaining_pinned_smoke_fix_renders_the_command() -> None:
    request = ModelDelegationHealthRequest.model_validate(
        {
            "correlation_id": "health",
            "quiet_minutes": 360,
            "total_rows": 100,
            "pin_only_rungs": [
                {
                    "backend_id": "local-heavy-reasoning",
                    "provider": "local",
                    "model": "fixture-model",
                    "tier": "local",
                }
            ],
            "backends": [
                {
                    "backend_id": "local-heavy-reasoning",
                    "accepted": 0,
                    "last": "",
                    "counts": {"h1": 1, "window": 1, "h24": 1},
                }
            ],
        }
    )
    finding = HandlerDelegationHealth().handle(request).findings[0]
    assert "{smoke}" not in finding.fix
    assert "{backend_id}" not in finding.fix
    assert load_rules().smoke_command in finding.fix
    command = finding.fix.split("smoke the rung pinned: ", 1)[1].split(";", 1)[0]
    argv = shlex.split(command)
    assert argv[argv.index("--backend-id") + 1] == "local-heavy-reasoning"
    assert argv[argv.index("--task-type") + 1] == "document"
    assert argv[argv.index("--bus") + 1] == "kafka"
    assert argv[argv.index("--locus") + 1] == "deployed-lane"
