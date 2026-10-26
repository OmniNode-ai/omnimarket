# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-10580 reason="test fixture asserts user and volume paths do not appear in generated prompts"; test-literal-ok: negative assertions only
# test-literal-ok: negative assertions only
"""Unit tests for session bootstrap Rev 7 additions.

Covers:
- EnumDodCheckType registry (C6 fix: no arbitrary commands)
- Cross-tick ID verification data structures
- build_pulse_prompt output content
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_session_bootstrap.dod_verification_registry import (
    DOD_VERIFICATION_REGISTRY,
    classify_evidence_kind,
    run_dod_check,
)
from omnimarket.nodes.node_session_bootstrap.handlers.handler_session_bootstrap import (
    build_pulse_prompt,
)
from omnimarket.nodes.node_session_bootstrap.models.model_task_contract import (
    EnumDodCheckType,
    EnumEvidenceArtifactKind,
    ModelDodEvidenceCheck,
    ModelTaskContract,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_contract() -> ModelTaskContract:
    return ModelTaskContract(
        task_id="build-8568",
        ticket_id="OMN-8568",
        target_repo="OmniNode-ai/omnimarket",
        target_branch_pattern="jonah/omn-8568-*",
        dod_evidence=[ModelDodEvidenceCheck(check_type=EnumDodCheckType.PR_OPENED)],
        dispatched_at=datetime.now(tz=UTC),
        dispatch_path="agent_bypass",
        model_used="sonnet",
    )


# ---------------------------------------------------------------------------
# EnumDodCheckType — C6 fix
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnumDodCheckType:
    """EnumDodCheckType is a closed enum: no arbitrary commands."""

    def test_all_values_present_in_registry(self) -> None:
        """Every EnumDodCheckType value must have a registry entry."""
        for check_type in EnumDodCheckType:
            assert check_type in DOD_VERIFICATION_REGISTRY, (
                f"{check_type!r} missing from DOD_VERIFICATION_REGISTRY"
            )

    def test_registry_rejects_unknown_string(self) -> None:
        """run_dod_check with a value not in the registry returns failure."""
        # EnumDodCheckType("rm -rf /") must raise — not a valid enum value
        with pytest.raises((ValueError, KeyError)):
            EnumDodCheckType("rm -rf /")

    def test_enum_values_are_safe_strings(self) -> None:
        """All enum values are simple identifiers — no shell metacharacters."""
        import re

        # Allow lowercase letters, digits, and underscores (e.g. overseer_5check)
        safe = re.compile(r"^[a-z0-9_]+$")
        for check_type in EnumDodCheckType:
            assert safe.match(check_type.value), (
                f"Unsafe enum value: {check_type.value!r}"
            )

    def test_model_dod_evidence_check_roundtrip(self) -> None:
        """ModelDodEvidenceCheck serializes and deserializes correctly."""
        check = ModelDodEvidenceCheck(
            check_type=EnumDodCheckType.PR_OPENED,
            required=True,
            timeout_seconds=30,
        )
        data = check.model_dump()
        assert data["check_type"] == "pr_opened"
        restored = ModelDodEvidenceCheck.model_validate(data)
        assert restored.check_type == EnumDodCheckType.PR_OPENED

    def test_model_task_contract_roundtrip(
        self, sample_contract: ModelTaskContract
    ) -> None:
        """ModelTaskContract serializes to JSON and back cleanly."""
        data = sample_contract.model_dump_json()
        parsed = ModelTaskContract.model_validate_json(data)
        assert parsed.task_id == sample_contract.task_id
        assert parsed.ticket_id == sample_contract.ticket_id
        assert parsed.dod_evidence[0].check_type == EnumDodCheckType.PR_OPENED

    def test_stall_timeout_seconds_defaults_none(
        self, sample_contract: ModelTaskContract
    ) -> None:
        """stall_timeout_seconds defaults to None (no override)."""
        assert sample_contract.stall_timeout_seconds is None

    def test_run_dod_check_pr_opened_without_gh(
        self, sample_contract: ModelTaskContract
    ) -> None:
        """run_dod_check(PR_OPENED) fails gracefully when gh is not configured."""
        # In test environment, gh may fail with non-zero exit or timeout.
        # We just verify the function returns (bool, str) without raising.
        passed, detail = run_dod_check(sample_contract, EnumDodCheckType.PR_OPENED)
        assert isinstance(passed, bool)
        assert isinstance(detail, str)

    def test_run_dod_check_rendered_output_always_passes(
        self, sample_contract: ModelTaskContract
    ) -> None:
        """rendered_output check returns True (deferred, phase 2)."""
        passed, detail = run_dod_check(
            sample_contract, EnumDodCheckType.RENDERED_OUTPUT
        )
        assert passed is True
        assert "deferred" in detail.lower()


# ---------------------------------------------------------------------------
# classify_evidence_kind / _check_rendered_output — OMN-13776 HTTP-probe fix
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestClassifyEvidenceKind:
    """HTTP-probe (curl/httpx) artifacts against non-UI endpoints classify as
    HTTP-evidence, not browser/UI evidence (OMN-13776); UI-class endpoints
    still require real rendered-output proof (no regression of OMN-13024).
    """

    def test_curl_against_ready_endpoint_is_http_evidence(self) -> None:
        kind, detail = classify_evidence_kind("curl -sf https://host/ready", "/ready")
        assert kind == EnumEvidenceArtifactKind.HTTP_EVIDENCE
        assert "HTTP-evidence" in detail

    def test_httpx_against_projection_api_is_http_evidence(self) -> None:
        kind, _ = classify_evidence_kind(
            "httpx GET https://host/v1/introspection/manifest",
            "/v1/introspection/manifest",
        )
        assert kind == EnumEvidenceArtifactKind.HTTP_EVIDENCE

    def test_wget_against_health_endpoint_is_http_evidence(self) -> None:
        kind, _ = classify_evidence_kind("wget -qO- https://host/health", "/health")
        assert kind == EnumEvidenceArtifactKind.HTTP_EVIDENCE

    def test_curl_against_dashboard_is_ui_rendered_no_regression(self) -> None:
        """curl-only proof against a UI/dashboard endpoint is still rejected
        (OMN-13024 regression guard)."""
        kind, detail = classify_evidence_kind(
            "curl -sf https://dash.dev.omninode.ai/", "https://dash.dev.omninode.ai/"
        )
        assert kind == EnumEvidenceArtifactKind.UI_RENDERED
        assert "OMN-13024" in detail

    def test_curl_against_dashboard_path_is_ui_rendered(self) -> None:
        kind, _ = classify_evidence_kind(
            "curl -sf https://host/dashboard", "/dashboard"
        )
        assert kind == EnumEvidenceArtifactKind.UI_RENDERED

    def test_missing_artifact_metadata_is_unknown(self) -> None:
        kind, detail = classify_evidence_kind(None, None)
        assert kind == EnumEvidenceArtifactKind.UNKNOWN
        assert "insufficient" in detail.lower()

    def test_non_probe_tool_against_api_endpoint_is_unknown(self) -> None:
        """A non-HTTP-probe command against a non-UI endpoint doesn't match
        either bucket — stays UNKNOWN rather than guessed."""
        kind, _ = classify_evidence_kind("psql -c 'select 1'", "/api/db")
        assert kind == EnumEvidenceArtifactKind.UNKNOWN


@pytest.mark.unit
class TestCheckRenderedOutputHttpProbeClassification:
    """_check_rendered_output (via run_dod_check) wires classify_evidence_kind
    into the RENDERED_OUTPUT DoD check."""

    def _contract_with_rendered_output(
        self, artifact_command: str | None, target_endpoint: str | None
    ) -> ModelTaskContract:
        return ModelTaskContract(
            task_id="build-13776",
            ticket_id="OMN-13776",
            target_repo="OmniNode-ai/omnimarket",
            target_branch_pattern="jonah/omn-13776-*",
            dod_evidence=[
                ModelDodEvidenceCheck(
                    check_type=EnumDodCheckType.RENDERED_OUTPUT,
                    artifact_command=artifact_command,
                    target_endpoint=target_endpoint,
                )
            ],
            dispatched_at=datetime.now(tz=UTC),
            dispatch_path="agent_bypass",
            model_used="sonnet",
        )

    def test_non_ui_http_probe_passes_without_playwright(self) -> None:
        contract = self._contract_with_rendered_output(
            "curl -sf https://host/ready", "/ready"
        )
        passed, detail = run_dod_check(contract, EnumDodCheckType.RENDERED_OUTPUT)
        assert passed is True
        assert "HTTP-evidence" in detail

    def test_ui_class_endpoint_still_requires_rendered_output(self) -> None:
        contract = self._contract_with_rendered_output(
            "curl -sf https://dash.dev.omninode.ai/", "https://dash.dev.omninode.ai/"
        )
        passed, detail = run_dod_check(contract, EnumDodCheckType.RENDERED_OUTPUT)
        assert passed is False
        assert "OMN-13024" in detail

    def test_no_artifact_metadata_preserves_deferred_behavior(self) -> None:
        contract = self._contract_with_rendered_output(None, None)
        passed, detail = run_dod_check(contract, EnumDodCheckType.RENDERED_OUTPUT)
        assert passed is True
        assert "deferred" in detail.lower()


# ---------------------------------------------------------------------------
# build_pulse_prompt — CronOutputVerificationRoutine content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildPulsePrompt:
    """build_pulse_prompt produces prompts with required Rev 7 content."""

    def test_prompt_contains_cross_tick_verification(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "Cross-tick verification" in prompt
        assert "HALLUCINATED PASS" in prompt
        assert "dispatch-events" in prompt

    def test_prompt_contains_vacuous_pulse_gate(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "VACUOUS_PULSE" in prompt
        assert "backlog_unworked_count" in prompt

    def test_prompt_has_no_dispatch_lease(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "dispatch-lock.json" not in prompt

    def test_prompt_uses_provided_state_dir(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir="/custom/state",
        )
        assert "/custom/state" in prompt

    def test_prompt_no_hardcoded_absolute_paths(self) -> None:
        """Prompt must not contain hardcoded /Users/jonah or /Volumes/ paths."""
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "/Users/jonah" not in prompt
        assert "/Volumes/" not in prompt

    def test_stall_thresholds_derived_from_budget(self) -> None:
        """Stall/dead thresholds in prompt are derived from timeout_budget_ms."""
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,  # 300s -> stall=99s, dead=198s
            session_id="sess-001",
            state_dir=".onex_state",
        )
        # 300 * 0.33 = 99, 300 * 0.66 = 198
        assert "99" in prompt
        assert "198" in prompt

    def test_generic_fallback_for_unknown_cron(self) -> None:
        prompt = build_pulse_prompt(
            cron_name="some-future-cron",
            timeout_budget_ms=120000,
            session_id="sess-002",
            state_dir=".onex_state",
        )
        assert "some-future-cron" in prompt
        assert "sess-002" in prompt

    def test_prompt_directs_health_violation_emission_on_hallucinated_pass(
        self,
    ) -> None:
        """Contract-state-coverage (OMN-13781): build_pulse_prompt's
        CronOutputVerificationRoutine guidance directs the agent to emit
        onex.evt.omnimarket.session-cron-health-violation.v1 both on a
        detected HALLUCINATED PASS and on a VACUOUS_PULSE gate failure —
        the node's declared output state is prompt-embedded, not
        code-published, so this is the executable coverage proof."""
        prompt = build_pulse_prompt(
            cron_name="build-dispatch-pulse",
            timeout_budget_ms=300000,
            session_id="sess-001",
            state_dir=".onex_state",
        )
        assert "onex.evt.omnimarket.session-cron-health-violation.v1" in prompt
        assert prompt.count("onex.evt.omnimarket.session-cron-health-violation.v1") >= 2
