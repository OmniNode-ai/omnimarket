# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the unpopulated local-share alarm out of the health contract."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml
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
_PACKAGED_BACKENDS = yaml.safe_load(
    (
        Path(__file__).parents[3] / "src/omnimarket/configs/bifrost_delegation.yaml"
    ).read_text()
)["backends"]
# A rung the packaged contract declares explicit_pin_only. Both local rungs are
# not pin-only, so they never belong in a pin-only rung list.
PIN_ONLY_BACKEND: str = next(
    b["backend_id"] for b in _PACKAGED_BACKENDS if b.get("explicit_pin_only")
)


def test_existing_caller_does_not_gain_an_unpopulated_local_share_warning() -> None:
    verdict = HandlerDelegationHealth().handle(
        ModelDelegationHealthRequest(
            correlation_id="health", quiet_minutes=360, total_rows=5
        )
    )
    assert not any("local share" in line for line in verdict.not_judged)


def test_rules_carry_no_local_share_field_and_refuse_one() -> None:
    # The rules model neither declares nor accepts the split-out local-share
    # fields, so a payload that names one is refused rather than ignored.
    payload = load_rules().model_dump(mode="json")
    assert not any("local_share" in key for key in payload)
    assert not any("local_share" in key for key in payload["fixes"])
    assert not any(
        "local_share" in name for name in ModelDelegationHealthRules.model_fields
    )
    ModelDelegationHealthRules.model_validate(payload)
    for extra in ({"local_share_window_minutes": 360}, {"local_share_min_runs": 5}):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ModelDelegationHealthRules.model_validate({**payload, **extra})
    fixes = {**payload["fixes"], "local_share_zero": "a fix"}
    with pytest.raises(ValidationError):
        ModelDelegationHealthRules.model_validate({**payload, "fixes": fixes})


def test_unimplemented_local_share_finding_is_not_advertised() -> None:
    assert "local_share_zero" not in {
        kind.value for kind in EnumDelegationHealthFindingKind
    }


def test_unread_local_share_counts_are_not_accepted_as_health_evidence() -> None:
    # One case is enough: the request model forbids the whole local_share block,
    # so the refusal does not depend on the run counts it carries.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ModelDelegationHealthRequest.model_validate(
            {
                "correlation_id": "health",
                "quiet_minutes": 360,
                "total_rows": 100,
                "local_share": {
                    "window_minutes": 360,
                    "delegated_runs": 5,
                    "local_runs": 0,
                    "local_endpoints_up": ["a point-in-time endpoint probe"],
                },
            }
        )


def test_the_smoke_fixture_rung_is_pin_only_in_the_packaged_contract() -> None:
    declared = {b["backend_id"]: b for b in _PACKAGED_BACKENDS}
    assert declared[PIN_ONLY_BACKEND]["explicit_pin_only"] is True
    for local in ("local-coder", "local-heavy-reasoning"):
        assert not declared[local].get("explicit_pin_only", False)


def test_remaining_pinned_smoke_fix_renders_the_command() -> None:
    request = ModelDelegationHealthRequest.model_validate(
        {
            "correlation_id": "health",
            "quiet_minutes": 360,
            "total_rows": 100,
            "pin_only_rungs": [
                {
                    "backend_id": PIN_ONLY_BACKEND,
                    "provider": "cloud",
                    "model": "fixture-model",
                    "tier": "cheap_cloud",
                }
            ],
            "backends": [
                {
                    "backend_id": PIN_ONLY_BACKEND,
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
    assert argv[argv.index("--backend-id") + 1] == PIN_ONLY_BACKEND
    assert argv[argv.index("--task-type") + 1] == "document"
    assert argv[argv.index("--bus") + 1] == "kafka"
    assert argv[argv.index("--locus") + 1] == "deployed-lane"
