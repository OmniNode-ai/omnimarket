# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation admission compute: golden parity and refusal sets (OMN-21097)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_delegation_admission_compute import (
    NodeDelegationAdmissionCompute,
)
from omnimarket.nodes.node_delegation_admission_compute.handlers.handler_delegation_admission import (
    DEFAULT_ADMISSION_POLICY,
    HandlerDelegationAdmission,
    effective_admission_policy,
)
from omnimarket.nodes.node_delegation_admission_compute.models.model_delegation_admission import (
    EnumAdmissionReason,
    EnumEffectiveMode,
    ModelCallOrigin,
    ModelDelegationAdmissionPolicy,
    ModelDelegationAdmissionRequest,
)

pytestmark = pytest.mark.unit

_TABLE = json.loads(
    (Path(__file__).parent / "fixtures" / "golden_admission_table.json").read_text(
        encoding="utf-8"
    )
)
_ROWS: list[dict[str, Any]] = _TABLE["rows"]
_CLASSIFIER_ROWS = [row for row in _ROWS if row["group"] == "classifier"]
_GUARD_ROWS = [row for row in _ROWS if row["group"] == "guard"]

# Rows where admission deliberately differs from the guard's reason. A fork is
# refused by both, but admission names the path (plan TB.2: forked calls are
# ``unsupported_path``) rather than the guard's model-inheritance reason.
_LISTED_DISAGREEMENTS: dict[str, EnumAdmissionReason] = {
    "guard-fork": EnumAdmissionReason.UNSUPPORTED_PATH,
    "guard-fork-opus": EnumAdmissionReason.UNSUPPORTED_PATH,
}

_MODEL_RAISED = ModelCallOrigin(plugin="engine", tier="core")
_ROUTE = "ROUTE: band=B3 score=9 dims=S2A1R1V2N1C1J1 dest=lane model=sonnet effort=high"


@pytest.fixture(scope="module")
def handler() -> HandlerDelegationAdmission:
    return NodeDelegationAdmissionCompute()


def _request(
    tool_input: dict[str, Any],
    *,
    origin: ModelCallOrigin | None = _MODEL_RAISED,
    mode: EnumEffectiveMode = EnumEffectiveMode.OBSERVE,
) -> ModelDelegationAdmissionRequest:
    return ModelDelegationAdmissionRequest(
        correlation_id="corr-1",
        tool_input=tool_input,
        call_origin=origin,
        effective_mode=mode,
    )


def _delegatable_input(
    text: str = "Research and summarize the routing tiers.",
) -> dict[str, Any]:
    return {
        "description": "research",
        "prompt": f"{_ROUTE}\n{text}",
        "subagent_type": "general-purpose",
        "model": "sonnet",
        "run_in_background": False,
    }


def test_golden_table_has_at_least_40_classifier_rows() -> None:
    assert len(_CLASSIFIER_ROWS) >= 40
    assert {row["omniclaude"]["delegatable"] for row in _CLASSIFIER_ROWS} == {
        True,
        False,
    }
    for row in _CLASSIFIER_ROWS:
        assert row["omniclaude"]["guard_reason"] is None, row["id"]
        assert row["omniclaude"]["sensitive"] is False, row["id"]


@pytest.mark.parametrize(
    "row", _CLASSIFIER_ROWS, ids=[r["id"] for r in _CLASSIFIER_ROWS]
)
def test_classifier_rows_reproduce_omniclaude_verdicts(
    handler: HandlerDelegationAdmission, row: dict[str, Any]
) -> None:
    result = handler.handle(_request(row["tool_input"]))
    expected = row["omniclaude"]
    if row["tool_input"].get("model") == "opus":
        assert result.admitted is False
        assert result.reason is EnumAdmissionReason.GUARD_MODEL_NOT_ALLOWED
        assert result.task_type == "unknown"
        return
    assert result.admitted is expected["delegatable"]
    assert result.task_type == expected["classified_intent"]
    assert result.reason is (
        EnumAdmissionReason.DELEGATABLE
        if expected["delegatable"]
        else EnumAdmissionReason.NOT_DELEGATABLE
    )


@pytest.mark.parametrize("row", _GUARD_ROWS, ids=[r["id"] for r in _GUARD_ROWS])
def test_guard_rows_keep_parity_with_the_agent_model_guard(
    handler: HandlerDelegationAdmission, row: dict[str, Any]
) -> None:
    result = handler.handle(_request(row["tool_input"]))
    expected = row["omniclaude"]
    if row["id"] in _LISTED_DISAGREEMENTS:
        assert expected["guard_reason"] is not None
        assert result.admitted is False
        assert result.reason is _LISTED_DISAGREEMENTS[row["id"]]
        return
    if row["tool_input"].get("model") == "opus":
        assert result.admitted is False
        assert result.reason is EnumAdmissionReason.GUARD_MODEL_NOT_ALLOWED
        assert result.reason_details == (
            "model 'opus' is not an allowed background model (allowed: sonnet)",
        )
        return
    if expected["guard_reason"] is not None:
        assert result.admitted is False
        assert result.reason.value.startswith("guard_")
        assert result.reason_details == (
            expected["guard_reason"].replace(
                "allowed: opus, sonnet", "allowed: sonnet"
            ),
        )
        return
    assert result.admitted is expected["delegatable"]
    assert result.task_type == expected["classified_intent"]


def test_listed_disagreements_are_rows_of_the_table() -> None:
    assert set(_LISTED_DISAGREEMENTS) <= {row["id"] for row in _GUARD_ROWS}


# One positive fixture per sensitivity pattern, assembled at runtime so no
# credential-shaped literal sits in the source.
_SENSITIVE_TEXTS: dict[str, str] = {
    "Bearer token detected": "Authorization: " + "Bearer " + "abc123def456ghi789",
    "Secret key (sk-) detected": "key " + "sk-" + "A" * 24,
    "Public/private key (pk_) detected": "key " + "pk_" + "B" * 24,
    "AWS access key (AKIA) detected": "id " + "AKIA" + "C" * 16,
    "GitHub personal access token detected": "tok " + "ghp_" + "D" * 36,
    "Slack API token detected": "tok " + "xox" + "b-" + "E" * 12,
    "Generic API key or token in key=value form": "api_key" + "=" + "F" * 20,
    "PEM private key block detected": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "Password or secret in key=value form": "pass" + "word=" + "G" * 8,
    "Sensitive environment variable assignment": "DB_" + "PASSWORD" + ": x",
    "Email address (PII) detected": "write to ops" + "@" + "example.com",
    "Phone number (PII) detected": "call 555-" + "867-" + "5309",
    "Social Security Number (PII) detected": "ssn 123-" + "45-" + "6789",
}


@pytest.mark.parametrize("label", sorted(_SENSITIVE_TEXTS))
def test_sensitive_inputs_are_not_admitted(
    handler: HandlerDelegationAdmission, label: str
) -> None:
    text = f"Research and summarize this. {_SENSITIVE_TEXTS[label]}"
    result = handler.handle(_request(_delegatable_input(text)))
    assert result.admitted is False
    assert result.reason is EnumAdmissionReason.SENSITIVE
    assert label in result.reason_details


def test_positive_control_admits_the_clean_base_input(
    handler: HandlerDelegationAdmission,
) -> None:
    result = handler.handle(_request(_delegatable_input()))
    assert result.admitted is True
    assert result.reason is EnumAdmissionReason.DELEGATABLE
    assert result.task_type == "research"


@pytest.mark.parametrize(
    ("case", "change"),
    [
        ("background-absent", {"run_in_background": None}),
        ("background-true", {"run_in_background": True}),
        ("fork", {"subagent_type": "fork"}),
        ("resume", {"resume": "agent-123"}),
    ],
)
def test_background_forked_and_resumed_calls_are_unsupported_path(
    handler: HandlerDelegationAdmission, case: str, change: dict[str, Any]
) -> None:
    tool_input = _delegatable_input()
    for key, value in change.items():
        if value is None:
            tool_input.pop(key)
        else:
            tool_input[key] = value
    result = handler.handle(_request(tool_input))
    assert result.admitted is False, case
    assert result.reason is EnumAdmissionReason.UNSUPPORTED_PATH


def test_plugin_raised_call_is_unsupported_path(
    handler: HandlerDelegationAdmission,
) -> None:
    origin = ModelCallOrigin(plugin="driver", tier="user")
    result = handler.handle(_request(_delegatable_input(), origin=origin))
    assert result.admitted is False
    assert result.reason is EnumAdmissionReason.UNSUPPORTED_PATH


def test_call_without_origin_is_unknown_call_origin(
    handler: HandlerDelegationAdmission,
) -> None:
    result = handler.handle(_request(_delegatable_input(), origin=None))
    assert result.admitted is False
    assert result.reason is EnumAdmissionReason.UNKNOWN_CALL_ORIGIN


@pytest.mark.parametrize("mode", list(EnumEffectiveMode))
def test_result_echoes_mode_and_policy_and_carries_no_prompt_text(
    handler: HandlerDelegationAdmission, mode: EnumEffectiveMode
) -> None:
    sentinel = "SENTINEL-7f3e9a-do-not-echo"
    result = handler.handle(
        _request(_delegatable_input(f"Research and summarize {sentinel}."), mode=mode)
    )
    assert result.effective_mode is mode
    assert result.policy_version == DEFAULT_ADMISSION_POLICY.policy_version
    assert result.correlation_id == "corr-1"
    assert sentinel not in result.model_dump_json()


def test_contract_admission_policy_equals_the_handler_policy() -> None:
    contract_path = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_delegation_admission_compute/contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    stated = ModelDelegationAdmissionPolicy.model_validate(contract["admission_policy"])
    assert effective_admission_policy(stated) == DEFAULT_ADMISSION_POLICY


def test_policy_matches_the_guard_allowlist_and_routing_bands() -> None:
    policy = DEFAULT_ADMISSION_POLICY
    assert set(policy.allowed_models) == {"sonnet"}
    assert policy.route_bands["B4"].models == ("sonnet",)
    assert policy.route_bands["B5"].models == ("sonnet",)
    assert policy.model_raised_origin == _MODEL_RAISED
    assert policy.route_required is True
    assert set(policy.route_bands) == {"B0", "B1", "B1c", "B2", "B3", "B4", "B5"}


def test_b4_sonnet_is_admitted_and_opus_is_refused(handler):
    prompt = "ROUTE: band=B4 score=14 dims=S2A2R2V2N2C2J2 dest=lane model=sonnet effort=high\nResearch routing."
    admitted = handler.handle(_request({**_delegatable_input(), "prompt": prompt}))
    assert admitted.admitted
    refused = handler.handle(
        _request(
            {
                **_delegatable_input(),
                "model": "opus",
                "prompt": prompt.replace("model=sonnet", "model=opus"),
            }
        )
    )
    assert not refused.admitted
    assert refused.reason is EnumAdmissionReason.GUARD_MODEL_NOT_ALLOWED
