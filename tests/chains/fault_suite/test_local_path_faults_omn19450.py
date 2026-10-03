# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider and gate faults through the real local delegation chain (OMN-19450).

Every run substitutes a registered customer key for the house OpenRouter rung.
The ladder has no successors; any same-backend retries hit the same faulty
loopback provider. No model judge scores an answer (OMN-20164), so gate
refusals must name a blocking rule of the deterministic gate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import EnumDelegationTerminalFailureCause

from omnimarket.enums.enum_delegation_acceptance import EnumDelegationAcceptanceReason
from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.inference.provider_finish_reason import TRUNCATION_CHECK_NAME
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
    ModelDelegateSkillResponse,
)
from omnimarket.routing import byok_provider_backends
from tests.chains.local.harness import (
    BYOK_BACKEND_ID,
    PROVIDER_SLUG,
    TASK_TYPE,
    LocalProviderStub,
    house_openrouter_rung,
    install_rungs,
    local_byok_catalogue,
    no_ambient_provider_credentials,
    run_local_delegation,
    shipped_byok_model_name,
    use_local_store,
)

pytestmark = [pytest.mark.unit, pytest.mark.local_chain]

_CUSTOMER_KEY = "sk-or-omn19450-fault-suite-customer-value"
_FAULTS = ("wrong_key", "quota", "timeout", "gate_veto", "truncation")
_EXPECTED_CAUSES = {
    "wrong_key": EnumDelegationTerminalFailureCause.AUTH_FAILED,
    "quota": EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED,
    "timeout": EnumDelegationTerminalFailureCause.TIMEOUT,
    "gate_veto": EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED,
    "truncation": EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED,
}
_PROMPT = "write a function parse_semver that parses a semver string"
#: The veto fault runs as a ``document`` task, whose class declares the
#: BLOCKING ``no_refusal`` heuristic; ``code_generation`` declares none, so a
#: refusal there is not vetoed by a named rule.
_VETO_TASK_TYPE = "document"
_VETO_PROMPT = "write a short paragraph describing what a semantic version is"
_ANSWER = (
    "### ANSWER\n"
    "def parse_semver(value):\n"
    "    return tuple(int(part) for part in value.split('.'))\n"
)
_VETO_RULE = "no_refusal"


def _deciding_attempt(
    response: ModelDelegateSkillResponse,
) -> ModelDelegateSkillAttemptRecord:
    """Use the last acceptance verdict, or the last transport-only attempt."""
    assert response.attempts, "the real chain must report its attempt evidence"
    return next(
        (
            attempt
            for attempt in reversed(response.attempts)
            if attempt.acceptance_reason is not None
        ),
        response.attempts[-1],
    )


async def _run_fault(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> ModelDelegateSkillResponse:
    """Configure only deployment seams and provider behavior; return the terminal."""
    assert fault in _FAULTS
    work_dir = tmp_path / fault
    work_dir.mkdir()
    with monkeypatch.context() as patch, no_ambient_provider_credentials(patch):
        stub = LocalProviderStub(model_id=shipped_byok_model_name(), content=_ANSWER)
        if fault == "wrong_key":
            stub.completion_status = 401
        elif fault == "quota":
            stub.completion_status = 429
            stub.completion_error_body = {
                "error": {
                    "message": "rate limit exceeded: quota exceeded",
                    "type": "insufficient_quota",
                    "code": 429,
                }
            }
        elif fault == "timeout":
            stub.completion_delay_seconds = 5.0
        elif fault == "gate_veto":
            # A refusal is not an answer: ``document`` carries ``no_refusal``
            # as a BLOCKING heuristic, so this answer is vetoed by a named rule.
            stub.content = "### ANSWER\nI cannot help with that request."
        elif fault == "truncation":
            stub.finish_reason = "length"

        stub.start()
        try:
            db_path = use_local_store(patch, work_dir)
            catalogue = local_byok_catalogue(patch, work_dir, stub.completions_url)
            rung = house_openrouter_rung(patch)
            if fault == "gate_veto":
                rung["capabilities"] = [_VETO_TASK_TYPE]
                install_rungs(patch, [rung])
            if fault == "timeout":
                rung["timeout_ms"] = 1500
                install_rungs(patch, [rung])
                # The BYOK row's explicit budget takes precedence over the
                # substituted house rung. Shorten that temporary copy too;
                # keep its shipped retry policy and model selection intact.
                document = yaml.safe_load(catalogue.read_text())
                for row in document["providers"]:
                    if row["provider"] == PROVIDER_SLUG:
                        row["timeout_ms"] = rung["timeout_ms"]
                catalogue.write_text(yaml.safe_dump(document, sort_keys=False))
                byok_provider_backends.load_byok_provider_catalog.cache_clear()
                byok_provider_backends.load_byok_plan_catalog.cache_clear()

            register_local_byok_credential(
                PROVIDER_SLUG, _CUSTOMER_KEY, db_path=db_path
            )
            response = await run_local_delegation(
                prompt=_VETO_PROMPT if fault == "gate_veto" else _PROMPT,
                task_type=_VETO_TASK_TYPE if fault == "gate_veto" else TASK_TYPE,
                db_path=db_path,
                correlation_id=uuid4(),
            )
            # Retain evidence before assertions so a RED run remains readable.
            record_property(
                f"fault_evidence_{fault}",
                json.dumps(response.model_dump(mode="json"), sort_keys=True),
            )
            assert stub.presented_credential == _CUSTOMER_KEY
            assert response.attempts[0].backend_id == BYOK_BACKEND_ID
            return response
        finally:
            stub.stop()
            byok_provider_backends.load_byok_provider_catalog.cache_clear()
            byok_provider_backends.load_byok_plan_catalog.cache_clear()
            byok_provider_backends.load_byok_not_offered_providers.cache_clear()


def _assert_failed(response: ModelDelegateSkillResponse, fault: str) -> None:
    assert response.status == "failed", response.model_dump(mode="json")
    assert response.terminal_failure_cause is _EXPECTED_CAUSES[fault], (
        response.model_dump(mode="json")
    )


async def test_wrong_key_is_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    response = await _run_fault("wrong_key", monkeypatch, tmp_path, record_property)
    _assert_failed(response, "wrong_key")


async def test_http_429_quota_body_is_provider_quota_exhausted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    response = await _run_fault("quota", monkeypatch, tmp_path, record_property)
    _assert_failed(response, "quota")


async def test_provider_delay_beyond_rung_timeout_is_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    response = await _run_fault("timeout", monkeypatch, tmp_path, record_property)
    _assert_failed(response, "timeout")


async def test_blocking_no_refusal_rule_is_quality_gate_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    response = await _run_fault("gate_veto", monkeypatch, tmp_path, record_property)
    _assert_failed(response, "gate_veto")
    deciding = _deciding_attempt(response)
    assert deciding.acceptance_reason is EnumDelegationAcceptanceReason.HEURISTIC_VETO
    assert f"vetoed_by={_VETO_RULE}:" in deciding.acceptance_detail


async def test_finish_reason_length_is_quality_gate_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    response = await _run_fault("truncation", monkeypatch, tmp_path, record_property)
    _assert_failed(response, "truncation")
    deciding = _deciding_attempt(response)
    assert TRUNCATION_CHECK_NAME == "not_truncated_by_output_budget"
    assert TRUNCATION_CHECK_NAME in deciding.acceptance_detail
    assert "finish_reason=length" in deciding.acceptance_detail
    if {
        "finish_reason",
        "truncated",
    } <= ModelDelegateSkillAttemptRecord.model_fields.keys():
        assert deciding.finish_reason == "length"
        assert deciding.truncated is True


@pytest.mark.parametrize("faults", [pytest.param(_FAULTS, id="all-five-faults")])
async def test_the_five_faults_have_pairwise_distinct_evidence_and_none_reads_provider_error(
    faults: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """Compare all five in one parametrized batch, using the same real-chain rig."""
    responses = {
        fault: await _run_fault(fault, monkeypatch, tmp_path, record_property)
        for fault in faults
    }
    for fault, response in responses.items():
        assert response.terminal_failure_cause is not (
            EnumDelegationTerminalFailureCause.PROVIDER_ERROR
        ), (fault, response.model_dump(mode="json"))
        _assert_failed(response, fault)
    assert (
        len(
            {
                responses[fault].terminal_failure_cause
                for fault in ("wrong_key", "quota", "timeout")
            }
        )
        == 3
    )
    veto = _deciding_attempt(responses["gate_veto"])
    truncation = _deciding_attempt(responses["truncation"])
    assert f"vetoed_by={_VETO_RULE}:" in veto.acceptance_detail
    assert f"vetoed_by={TRUNCATION_CHECK_NAME}:" in truncation.acceptance_detail
    veto_rule = veto.acceptance_detail.split("vetoed_by=", 1)[1].split(":", 1)[0]
    truncation_rule = truncation.acceptance_detail.split("vetoed_by=", 1)[1].split(
        ":", 1
    )[0]
    assert (
        veto.acceptance_reason != truncation.acceptance_reason
        or veto_rule != truncation_rule
    )
