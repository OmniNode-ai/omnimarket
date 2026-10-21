# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex models`` offers every provider the BYOK catalogue offers (OMN-20157).

The customer catalogue (``configs/byok_provider_backends.v1.yaml``) offers z.ai's
general API as ``glm``, and ``onex secret set llm.glm.api_key`` already stores a
key for it, but ``onex models list`` on a fresh profile named Gemini, OpenRouter,
OpenAI, Anthropic, Ollama and a local URL, and no GLM. The provider list of
``onex models`` is now checked against the catalogue, so a provider the
catalogue offers cannot be left out of it again.

Each test names the failure it exists to catch:

* a provider the catalogue offers a customer is missing from ``onex models``;
* ``onex models list`` on a fresh profile does not show GLM;
* a GLM key is tested on a backend other than the catalogue's default plan, or
  the endpoint is written into the setup code instead of read from the
  catalogue;
* a Coding Plan key (detection-only, barred by z.ai's terms) is stored, or the
  refusal hides its typed code;
* a general-API key is not stored under its detected plan and model.

Fakes only: plan detection and the pinned delegation are stubs; no network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.cli.cli_secret import models_group
from omnimarket.inference.local_byok_credential_adapter import (
    LocalByokCredentialStore,
    registered_local_byok_providers,
    resolve_local_byok_credential_model,
)
from omnimarket.nodes.node_model_setup_effect.handlers import handler_model_setup
from omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup import (
    PROVIDERS,
    HandlerModelSetup,
)
from omnimarket.routing.byok_plan_detection import ModelByokPlanDetection
from omnimarket.routing.byok_provider_backends import (
    byok_routable_plans,
    customer_provider_catalogue,
    resolve_byok_provider_backend,
)

pytestmark = pytest.mark.unit

_KEY = "0123456789abcdef0123456789abcdef.synthetic-omn20157-never-printed"


@pytest.fixture(autouse=True)
def local_store_at_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation" / "delegation.sqlite"
    db_path.parent.mkdir()
    for module in (
        "omnimarket.inference.local_byok_credential_adapter",
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup",
    ):
        monkeypatch.setattr(f"{module}.default_evidence_db_path", lambda: db_path)
    return db_path


class _Detect:
    """Stands in for plan detection; records that it was asked."""

    def __init__(self, detection: ModelByokPlanDetection) -> None:
        self.detection = detection
        self.calls: list[str] = []

    async def __call__(self, provider: str, api_key: Any, **_: Any) -> Any:
        self.calls.append(provider)
        return self.detection


class _Delegate:
    """Stands in for ``onex delegate --backend-id``; records each pin it got."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.pins: list[str] = []

    def __call__(self, backend_id: str) -> tuple[int, str, str]:
        self.pins.append(backend_id)
        row = resolve_byok_provider_backend("glm")
        assert row is not None
        receipt = self.tmp_path / f"run-{len(self.pins)}" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": "glm-5.3-flash",
                    "endpoint": row.endpoint_url,
                }
            ),
            encoding="utf-8",
        )
        return 0, '{"status": "success"}\n', f"delegate artifacts: {receipt}"


def _general_api() -> ModelByokPlanDetection:
    return ModelByokPlanDetection(
        provider="glm", plan="general_api", outcome="detected", model="glm-5.3-flash"
    )


def _coding_plan_only() -> ModelByokPlanDetection:
    return ModelByokPlanDetection(
        provider="glm",
        plan=None,
        outcome="not_permitted",
        refused_plan="coding_plan",
        refusal_code="BYOK_CODING_PLAN_NOT_PERMITTED",
    )


def _arrange(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, detection: ModelByokPlanDetection
) -> tuple[_Detect, _Delegate]:
    detect = _Detect(detection)
    delegate = _Delegate(tmp_path)
    monkeypatch.setattr(cli_secret, "detect_byok_plan", detect)
    monkeypatch.setattr(handler_model_setup, "run_pinned_delegation", delegate)
    return detect, delegate


def _add(*args: str, stdin: str = f"{_KEY}\n") -> Any:
    # The customer chooses the model a key runs (OMN-21059).
    return CliRunner().invoke(
        models_group, ["add", "glm", "--model", "glm-5.3-flash", *args], input=stdin
    )


class TestTheProviderListFollowsTheCatalogue:
    def test_every_catalogue_provider_a_customer_can_route_is_offered(self) -> None:
        routable = {p for p in customer_provider_catalogue() if byok_routable_plans(p)}
        missing = sorted(routable - set(PROVIDERS))
        assert missing == [], f"onex models does not offer {missing}"

    def test_glm_is_in_the_catalogue_and_in_onex_models(self) -> None:
        assert "glm" in customer_provider_catalogue()
        assert "glm" in PROVIDERS

    def test_a_fresh_profile_lists_glm_not_set_up(self) -> None:
        result = CliRunner().invoke(models_group, ["list"])
        assert result.exit_code == 0, result.output
        assert "GLM         not set up" in result.output.splitlines()

    def test_a_fresh_profile_lists_every_offered_provider(self) -> None:
        result = CliRunner().invoke(models_group, ["list", "--json"])
        assert result.exit_code == 0, result.output
        listed = [json.loads(line)["provider"] for line in result.output.splitlines()]
        assert listed == list(PROVIDERS)
        assert all(
            not json.loads(line)["set_up"] for line in result.output.splitlines()
        )


class TestTheGlmRouteComesFromTheCatalogue:
    def test_glm_tests_on_the_catalogue_default_plan_backend(self) -> None:
        row = resolve_byok_provider_backend("glm")
        assert row is not None
        assert row.plan == "general_api"
        assert HandlerModelSetup.backend_for("glm") == row.backend_id

    def test_no_glm_endpoint_is_written_in_the_setup_code(self) -> None:
        row = resolve_byok_provider_backend("glm")
        assert row is not None
        for module in (cli_secret, handler_model_setup):
            source = Path(str(module.__file__)).read_text(encoding="utf-8")
            assert row.endpoint_url not in source
            assert "api.z.ai" not in source


class TestModelsAddGlm:
    def test_a_general_api_key_is_stored_and_tested_on_its_backend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        detect, delegate = _arrange(monkeypatch, tmp_path, _general_api())
        result = _add()

        assert result.exit_code == 0, result.output
        assert detect.calls == ["glm"]
        stored = asyncio.run(LocalByokCredentialStore().get_secret("llm.glm.api_key"))
        assert stored == _KEY
        assert "glm" in registered_local_byok_providers()
        assert resolve_local_byok_credential_model("glm") == "glm-5.3-flash"
        assert delegate.pins == ["byok-glm-general"]
        assert "GLM         ✓ answered (glm-5.3-flash)" in result.output

    def test_list_shows_glm_set_up_after_add(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _arrange(monkeypatch, tmp_path, _general_api())
        assert _add().exit_code == 0

        result = CliRunner().invoke(models_group, ["list"])
        glm = [line for line in result.output.splitlines() if line.startswith("GLM")]
        assert len(glm) == 1
        assert "set up, last test passed" in glm[0]
        assert "glm-5.3-flash" in glm[0]

    def test_a_coding_plan_key_is_refused_with_its_typed_code_and_not_stored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _, delegate = _arrange(monkeypatch, tmp_path, _coding_plan_only())
        result = _add()

        assert result.exit_code != 0
        assert "BYOK_CODING_PLAN_NOT_PERMITTED" in result.output
        assert "Nothing was stored." in result.output
        assert asyncio.run(LocalByokCredentialStore().list_keys()) == []
        assert "glm" not in registered_local_byok_providers()
        assert delegate.pins == []

    def test_the_key_is_never_echoed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _arrange(monkeypatch, tmp_path, _general_api())
        assert _KEY not in _add().output
        _arrange(monkeypatch, tmp_path, _coding_plan_only())
        assert _KEY not in _add().output
