# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A probe rung may declare its credential absent; the walk skips it (OMN-23170)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_probe_credential_ladder import (
    ModelProbeCredentialLadder,
    ModelProbeCredentialRung,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    ModelProbeLadder,
    ModelProbeRung,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    _ProbeLadderWalk,
)


@pytest.fixture(name="loopback_endpoint")
def loopback_endpoint() -> str:
    return "http://127.0.0.1:9/v1/chat/completions"


def _rung(backend_id: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "backend_id": backend_id,
        "model_id": f"stub-{backend_id}",
        "endpoint_ref": "http://127.0.0.1:9/v1/chat/completions",
    }
    payload.update(overrides)
    return payload


def test_released_models_keep_exactly_their_keys() -> None:
    """The released rung and ladder keep exactly their keys (the gate control)."""
    assert sorted(ModelProbeRung.model_fields) == [
        "backend_id",
        "endpoint_ref",
        "max_tokens",
        "model_id",
        "tier",
        "timeout_ms",
    ]
    assert sorted(ModelProbeLadder.model_fields) == ["rungs"]


def test_credential_rung_defaults_resolved_and_released_ladder_refuses_the_key() -> (
    None
):
    rung = ModelProbeCredentialRung.model_validate(_rung("a"))
    assert rung.credential_resolved is True
    with pytest.raises(ValidationError):
        ModelProbeLadder.model_validate(
            {"rungs": [_rung("a", credential_resolved=False), _rung("b")]}
        )
    ladder = ModelProbeCredentialLadder.model_validate(
        {"rungs": [_rung("a", credential_resolved=False), _rung("b")]}
    )
    assert ladder.rungs[0].credential_resolved is False


def test_request_decodes_a_credential_ladder_only_when_a_rung_declares_it() -> None:
    def request(probe_ladder: dict[str, object]) -> ModelDelegateSkillRequest:
        return ModelDelegateSkillRequest.model_validate(
            {
                "prompt": "p",
                "task_type": "test",
                "source": "claude-code",
                "provenance": {
                    "source": "claude-code",
                    "traffic_class": "synthetic",
                    "source_surface": "readiness_probe",
                },
                "probe_ladder": probe_ladder,
            }
        )

    assert isinstance(
        request(
            {"rungs": [_rung("a", credential_resolved=False), _rung("b")]}
        ).probe_ladder,
        ModelProbeCredentialLadder,
    )
    assert isinstance(request({"rungs": [_rung("a")]}).probe_ladder, ModelProbeLadder)


def test_credential_ladder_refuses_all_absent_and_repeated_backends() -> None:
    with pytest.raises(ValidationError):
        ModelProbeCredentialLadder.model_validate(
            {"rungs": [_rung("a", credential_resolved=False)]}
        )
    ladder = ModelProbeCredentialLadder.model_validate(
        {
            "rungs": [
                _rung("a"),
                _rung("b", credential_resolved=False),
                _rung("c"),
            ]
        }
    )
    assert [rung.credential_resolved for rung in ladder.rungs] == [True, False, True]
    with pytest.raises(ValidationError):
        ModelProbeCredentialLadder.model_validate(
            {"rungs": [_rung("a"), _rung("a", credential_resolved=False)]}
        )


def test_walk_skips_an_absent_credential_rung() -> None:
    ladder = ModelProbeCredentialLadder.model_validate(
        {
            "rungs": [
                _rung("slow"),
                _rung("absent", credential_resolved=False),
                _rung("healthy"),
            ]
        }
    )
    walk = _ProbeLadderWalk(ladder)
    assert walk.length == 2
    assert walk.first().backend_id == "slow"
    assert walk.next().backend_id == "healthy"
    assert walk.next() is None
