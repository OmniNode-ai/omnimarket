# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157: ``onex secret set`` files a multi-plan key under the right plan.

The local half of onboarding. Detection itself is pinned in
``tests/test_byok_plan_detection.py``; here the CLI is checked to (1) record the
plan it is told or finds, (2) route the substitution to THAT plan's endpoint,
(3) store nothing when no plan can be decided, and (4) never leak the key.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    resolve_local_byok_credential_plan,
    resolve_local_byok_credential_ref,
)
from omnimarket.routing.byok_plan_detection import ModelByokPlanDetection
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import substitute_local_byok_route

pytestmark = pytest.mark.unit

_GLM_REF = "llm.glm.api_key"
_GEMINI_REF = "llm.gemini.api_key"
_KEY = "customer-key-that-must-never-be-echoed-0123"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    return db_path


def _detect_returns(
    monkeypatch: pytest.MonkeyPatch, detection: ModelByokPlanDetection
) -> list[str]:
    calls: list[str] = []

    async def _fake(
        provider: str, api_key: object, **_: object
    ) -> ModelByokPlanDetection:
        calls.append(provider)
        return detection

    monkeypatch.setattr("omnimarket.cli.cli_secret.detect_byok_plan", _fake)
    return calls


def _run(args: list[str]) -> object:
    return CliRunner().invoke(
        secret_group, args, input=f"{_KEY}\n", catch_exceptions=False
    )


def _house_rung(
    backend_id: str, endpoint: str, model: str, secret_ref: str
) -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id=backend_id,
        model_id=model,
        endpoint_ref=endpoint,
        tier="cheap_cloud",
        max_tokens=1024,
        timeout_ms=240000,
        secret_ref=secret_ref,
    )


def _glm_house() -> ModelResolvedDelegationBackend:
    return _house_rung(
        "cloud-glm",
        "https://api.z.ai/api/coding/paas/v4/chat/completions",
        "glm-5.3-flash",
        _GLM_REF,
    )


def test_a_detected_general_api_key_routes_to_the_general_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="glm", plan="general_api", outcome="detected"),
    )

    result = _run(["set", _GLM_REF])

    assert result.exit_code == 0, result.output
    assert "Detected plan: general_api" in result.output
    assert resolve_local_byok_credential_plan("glm") == "general_api"
    routed = substitute_local_byok_route(_glm_house())
    assert routed.backend_id == "byok-glm-general"
    assert routed.endpoint_ref == "https://api.z.ai/api/paas/v4/chat/completions"
    assert routed.model_id == "glm-4.5-flash"


def test_a_coding_plan_only_key_is_refused_with_the_typed_code_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # z.ai's terms bar Coding Plan quota from third-party systems (knowledge-base-
    # internal reference/zai-glm-coding-plan-terms.md), so a key that answers only
    # there is refused and the customer is told to register a general API key.
    _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(
            provider="glm",
            plan=None,
            outcome="not_permitted",
            refused_plan="coding_plan",
            refusal_code="BYOK_CODING_PLAN_NOT_PERMITTED",
        ),
    )

    result = _run(["set", _GLM_REF])

    assert result.exit_code != 0
    assert "BYOK_CODING_PLAN_NOT_PERMITTED" in result.output
    assert "general API key" in result.output
    assert "Nothing was stored" in result.output
    assert _KEY not in result.output
    assert asyncio.run(LocalByokCredentialStore().list_keys()) == []
    assert resolve_local_byok_credential_ref("glm") is None


def test_a_detector_that_names_the_coding_plan_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="glm", plan="coding_plan", outcome="detected"),
    )

    result = _run(["set", _GLM_REF])

    assert result.exit_code != 0
    assert "BYOK_CODING_PLAN_NOT_PERMITTED" in result.output
    assert resolve_local_byok_credential_ref("glm") is None


def test_naming_the_coding_plan_is_refused_without_any_network_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="glm", plan=None, outcome="inconclusive"),
    )

    result = _run(["set", _GLM_REF, "--plan", "coding_plan"])

    assert result.exit_code != 0
    assert calls == [], "--plan must send the key nowhere"
    assert "BYOK_CODING_PLAN_NOT_PERMITTED" in result.output
    assert "general API key" in result.output
    assert asyncio.run(LocalByokCredentialStore().list_keys()) == []


def test_a_key_with_no_plan_named_and_no_detection_needed_registers_the_general_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A key both surfaces answer is reported by detection as a general key.
    _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="glm", plan="general_api", outcome="detected"),
    )

    result = _run(["set", _GLM_REF])

    assert result.exit_code == 0, result.output
    routed = substitute_local_byok_route(_glm_house())
    assert routed.backend_id == "byok-glm-general"
    assert "/api/coding/" not in routed.endpoint_ref


def test_an_explicit_plan_is_recorded_without_any_network_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="glm", plan=None, outcome="inconclusive"),
    )

    result = _run(["set", _GLM_REF, "--plan", "general_api"])

    assert result.exit_code == 0, result.output
    assert calls == [], "--plan must send the key nowhere"
    assert resolve_local_byok_credential_plan("glm") == "general_api"


def test_an_unknown_plan_is_refused_and_nothing_is_stored() -> None:
    result = _run(["set", _GLM_REF, "--plan", "enterprise"])

    assert result.exit_code != 0
    # The plans offered back are the ones a customer may register under.
    assert "general_api" in result.output
    assert asyncio.run(LocalByokCredentialStore().list_keys()) == []


@pytest.mark.parametrize("outcome", ["rejected", "inconclusive", "ambiguous"])
def test_an_undecided_plan_stores_nothing(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="glm", plan=None, outcome=outcome),  # type: ignore[arg-type]
    )

    result = _run(["set", _GLM_REF])

    assert result.exit_code != 0
    assert asyncio.run(LocalByokCredentialStore().list_keys()) == []
    assert resolve_local_byok_credential_ref("glm") is None
    if outcome in {"inconclusive", "ambiguous"}:
        assert "--plan" in result.output
    if outcome == "ambiguous":
        assert "will not choose" in result.output


def test_the_key_is_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="glm", plan=None, outcome="rejected"),
    )
    failed = _run(["set", _GLM_REF])
    assert _KEY not in failed.output

    _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="glm", plan="general_api", outcome="detected"),
    )
    ok = _run(["set", _GLM_REF])
    assert _KEY not in ok.output


def test_a_single_plan_provider_is_never_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _detect_returns(
        monkeypatch,
        ModelByokPlanDetection(provider="gemini", plan=None, outcome="rejected"),
    )

    result = _run(["set", _GEMINI_REF])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert resolve_local_byok_credential_ref("gemini") is not None


def test_a_gemini_key_substitutes_the_customer_gemini_route() -> None:
    _run(["set", _GEMINI_REF])
    house = _house_rung(
        "cloud-gemini-flash",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "gemini-2.5-flash-lite",
        _GEMINI_REF,
    )

    routed = substitute_local_byok_route(house)

    assert routed.backend_id == "byok-gemini"
    assert routed.model_id == "gemini-2.5-flash-lite"
    assert routed.secret_ref is not None
    assert routed.secret_ref.startswith("cred_localinstall_gemini_")
    assert asyncio.run(LocalByokCredentialStore().get_secret(routed.secret_ref)) == _KEY


@pytest.mark.parametrize("recorded_plan", ["retired_plan", "coding_plan"])
def test_a_recorded_plan_the_catalogue_does_not_route_fails_closed(
    monkeypatch: pytest.MonkeyPatch, recorded_plan: str
) -> None:
    _run(["set", _GLM_REF, "--plan", "general_api"])
    from omnimarket.inference import local_byok_credential_adapter as adapter

    monkeypatch.setattr(
        "omnimarket.routing.local_byok_route.resolve_local_byok_credential_plan",
        lambda _provider, **_kw: recorded_plan,
    )
    house = _glm_house()
    assert substitute_local_byok_route(house) == house
    assert adapter.resolve_local_byok_credential_ref("glm") is not None


def test_a_database_from_before_plans_gains_the_column_and_reads_as_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    legacy = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE local_inference_credentials (secret_ref TEXT PRIMARY KEY, "
        "provider TEXT NOT NULL, secret_value TEXT NOT NULL, "
        "registered_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute(
        "INSERT INTO local_inference_credentials (secret_ref, provider, secret_value) "
        "VALUES ('cred_localinstall_glm_ab', 'glm', 'v')"
    )
    conn.commit()
    conn.close()
    legacy.chmod(0o600)
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: legacy,
    )

    assert resolve_local_byok_credential_plan("glm") is None
    routed = substitute_local_byok_route(_glm_house())
    assert routed.backend_id == "byok-glm-general"
