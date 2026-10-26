# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serving identity must distinguish hosts and quantizations sharing an API alias."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.model_backend_identity import ModelBackendIdentity
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelBifrostDelegationConfig,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    _attempt_configuration,
)
from omnimarket.routing.delegation_backend_resolution import resolve_delegation_backend

pytestmark = pytest.mark.unit


def identity(host="inference.example", quant="Q8_0"):
    return {
        "host": host,
        "model_file": f"model-{quant}.gguf",
        "quant": quant,
        "context_tokens": 65536,
    }


def backend(**updates):
    value = {
        "backend_id": "local-a",
        "provider": "local",
        "endpoint_url": "http://inference.example:8130/v1/chat/completions",
        "model_name": "shared-model",
        "tier": "local",
        "max_tokens": 8192,
        "timeout_ms": 60000,
        "capabilities": ["documentation"],
        "serving_identity": identity(),
    }
    value.update(updates)
    return value


def config(backends):
    return ModelBifrostDelegationConfig.model_validate(
        {
            "config_version": "1.0.0",
            "schema_version": "bifrost_delegation.v1",
            "backends": backends,
            "routing_rules": [
                {
                    "rule_id": "00000000-0000-0000-0000-000000000001",
                    "task_class": "document",
                    "task_class_contract_version": "1.0.0",
                    "backend_policy_version": "1.0.0",
                    "fallback_policy": {"action": "return_error"},
                    "shadow_policy_id": "00000000-0000-0000-0000-000000000002",
                    "backend_ids": [backends[0]["backend_id"]],
                }
            ],
        }
    )


def test_identity_rejects_blank_fields_and_nonpositive_context():
    for field in ("host", "model_file", "quant"):
        with pytest.raises(ValidationError):
            ModelBackendIdentity.model_validate({**identity(), field: " \t "})
    with pytest.raises(ValidationError):
        ModelBackendIdentity.model_validate({**identity(), "context_tokens": 0})


def test_alias_cannot_hide_a_different_quant():
    other = backend(
        backend_id="local-b",
        endpoint_url="http://peer.example:8130/v1/chat/completions",
        serving_identity=identity("peer.example", "Q3_K_M"),
    )
    with pytest.raises(ValidationError, match="ambiguous_model_alias"):
        config([backend(), other])
    other["model_name"] = "shared-model-Q3_K_M"
    assert len(config([backend(), other]).backends) == 2


def test_identity_host_must_match_the_endpoint():
    with pytest.raises(ValidationError, match="identity_host_mismatch"):
        config([backend(serving_identity=identity("wrong.example"))])


def test_resolution_and_attempt_preserve_contract_identity():
    resolved = resolve_delegation_backend(
        "document", backend_id="local-a", backends=[backend()]
    )
    assert resolved.serving_identity == ModelBackendIdentity.model_validate(identity())
    params = _attempt_configuration(resolved, max_tokens=None, temperature=None)
    assert params["serving_identity"] == identity()
    attempt = ModelDelegateSkillAttemptRecord.model_validate(
        {
            "tier": "local",
            "backend_id": resolved.backend_id,
            "model_id": resolved.model_id,
            "host": "inference.example",
            "quality_gate_passed": True,
            "configuration": params,
        }
    )
    assert attempt.identity_status == "recorded"
    assert attempt.scoring_identity is not None
    replay = ModelDelegateSkillAttemptRecord.model_validate(
        attempt.model_dump(mode="json")
    )
    assert replay.scoring_identity == attempt.scoring_identity


def test_missing_identity_is_flagged_and_cannot_supply_a_score_key():
    attempt = ModelDelegateSkillAttemptRecord.model_validate(
        {
            "tier": "local",
            "backend_id": "local-a",
            "model_id": "shared-model",
            "quality_gate_passed": True,
        }
    )
    assert attempt.identity_status == "unrecorded"
    assert attempt.scoring_identity is None


def test_bare_backend_lists_cannot_bypass_alias_validation():
    other = backend(
        backend_id="local-b",
        endpoint_url="http://peer.example:8130/v1/chat/completions",
        serving_identity=identity("peer.example", "Q3_K_M"),
    )
    with pytest.raises(ValueError, match="ambiguous_model_alias"):
        resolve_delegation_backend(
            "document", backend_id="local-a", backends=[backend(), other]
        )


def test_score_key_distinguishes_quant_and_context():
    original = ModelBackendIdentity.model_validate(identity())
    changed = ModelBackendIdentity.model_validate(
        {**identity(), "context_tokens": 32768}
    )
    assert original.fingerprint != changed.fingerprint
    assert (
        original.fingerprint
        == ModelBackendIdentity.model_validate(deepcopy(identity())).fingerprint
    )


def test_bus_history_keeps_identity_and_replay_status():
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        _canonical_attempt_records,
    )

    records = _canonical_attempt_records(
        [
            {
                "tier_name": "local",
                "backend_ref": "local-a",
                "model_used": "shared-model",
                "host": "inference.example",
                "acceptance_decision": "accept",
                "configuration": {"serving_identity": identity()},
            }
        ]
    )
    assert records[0]["identity_status"] == "recorded"
    assert records[0]["configuration"]["serving_identity"] == identity()
