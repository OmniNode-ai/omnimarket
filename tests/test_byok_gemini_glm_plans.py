# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157: the BYOK catalogue offers gemini and two GLM plans, each with a limit model.

Three facts have to hold together:

1. ``gemini`` is an offered provider (API-key auth) and ``glm`` is offered as
   TWO plans -- the Coding Plan and the general API -- selected by a ``plan``
   the customer's credential records. Two z.ai surfaces, one request shape.
2. Every row declares a limit model (billing, counter scope, windows) so quota
   tracking counts per tenant + credential + provider + plan/model with the
   lab as one tenant among the rest (no separate lab path).
3. ``vertex`` stays not offered, and its reason says SSO-style auth is out of
   scope (direction relayed in the dispatch, not yet a RULING row) so the row
   reads as a decision and not a gap.

The general parity gate stays in ``tests/test_omn17353_provider_catalogue.py``;
this module pins what that gate cannot express.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.routing import byok_provider_backends as mod
from omnimarket.routing.byok_provider_backends import (
    CATALOG_PATH,
    ByokCatalogError,
    ModelByokLimitModel,
    byok_backend_max_retries,
    byok_limit_counter_key,
    byok_provider_plans,
    customer_provider_catalogue,
    house_keyed_provider_slugs,
    load_byok_not_offered_providers,
    load_byok_plan_catalog,
    load_byok_provider_catalog,
    resolve_byok_declared_plan,
    resolve_byok_provider_backend,
    select_byok_model,
)

pytestmark = pytest.mark.unit

BIFROST_CONTRACT_PATH = CATALOG_PATH.parent / "bifrost_delegation.yaml"

CODING_PLAN_ENDPOINT = "https://api.z.ai/api/coding/paas/v4/chat/completions"
GENERAL_API_ENDPOINT = "https://api.z.ai/api/paas/v4/chat/completions"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
)


@pytest.fixture(autouse=True)
def _fresh_caches() -> Any:
    for loader in (
        load_byok_provider_catalog,
        load_byok_plan_catalog,
        load_byok_not_offered_providers,
    ):
        loader.cache_clear()
    yield
    for loader in (
        load_byok_provider_catalog,
        load_byok_plan_catalog,
        load_byok_not_offered_providers,
    ):
        loader.cache_clear()


def _platform_backends() -> list[dict[str, Any]]:
    payload = yaml.safe_load(BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))
    return [b for b in payload.get("backends", []) if isinstance(b, dict)]


def _catalogue_payload() -> dict[str, Any]:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "catalogue.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


class TestGeminiIsOffered:
    def test_gemini_is_on_the_customer_catalogue_and_not_declared_not_offered(
        self,
    ) -> None:
        assert "gemini" in customer_provider_catalogue()
        assert "gemini" not in load_byok_not_offered_providers()

    def test_gemini_row_posts_the_complete_openai_compatible_url_verbatim(
        self,
    ) -> None:
        row = resolve_byok_provider_backend("gemini")
        assert row is not None
        assert row.endpoint_url == GEMINI_ENDPOINT
        assert row.backend_id == "byok-gemini"
        assert row.plan == "ai_studio"

    def test_gemini_row_mirrors_a_live_house_rung_of_the_same_provider(self) -> None:
        row = resolve_byok_provider_backend("gemini")
        assert row is not None
        mirrored = [
            b for b in _platform_backends() if b.get("endpoint_url") == row.endpoint_url
        ]
        assert mirrored, "the gemini row mirrors no bifrost rung"
        assert house_keyed_provider_slugs(mirrored) == frozenset({"gemini"})

    def test_gemini_default_is_the_cheap_slug_not_the_quota_zero_pro(self) -> None:
        row = resolve_byok_provider_backend("gemini")
        assert row is not None
        # OMN-13351: gemini-2.5-pro was quota-zero on the only resolvable key.
        # OMN-20157: no id is pinned; no preference entry may pick a pro model.
        listed = ["gemini-2.5-pro", "gemini-3.5-pro", "gemini-3.5-flash-lite"]
        assert select_byok_model(row, listed) == "gemini-3.5-flash-lite"
        assert select_byok_model(row, ["gemini-2.5-pro", "gemini-3.5-pro"]) is None
        assert row.max_tokens is not None
        assert row.max_tokens <= 8192, "flash-lite caps output near 8192 tokens"


class TestGlmHasTwoPlans:
    def test_glm_declares_exactly_the_coding_plan_and_the_general_api(self) -> None:
        assert byok_provider_plans("glm") == ("coding_plan", "general_api")

    def test_the_default_plan_is_the_general_api_not_the_coding_plan(
        self,
    ) -> None:
        # OMN-20157: z.ai's terms bar Coding Plan quota from third-party systems
        # (knowledge-base-internal reference/zai-glm-coding-plan-terms.md), so the
        # plan a customer key gets when none is named is the general API.
        default = resolve_byok_provider_backend("glm")
        assert default is not None
        assert default.plan == "general_api"
        assert default.backend_id == "byok-glm-general"
        assert default.endpoint_url == GENERAL_API_ENDPOINT
        assert load_byok_provider_catalog()["glm"] == default

    def test_each_declared_plan_resolves_its_own_endpoint_and_backend(self) -> None:
        coding = resolve_byok_declared_plan("glm", "coding_plan")
        general = resolve_byok_provider_backend("glm", plan="general_api")
        assert coding is not None
        assert general is not None
        assert coding.endpoint_url == CODING_PLAN_ENDPOINT
        assert general.endpoint_url == GENERAL_API_ENDPOINT
        assert coding.backend_id != general.backend_id
        assert general.backend_id == "byok-glm-general"
        # Declared for detection; never routable (see test_omn20157_glm_general_api_default).
        assert resolve_byok_provider_backend("glm", plan="coding_plan") is None

    def test_an_unknown_plan_resolves_to_nothing_and_never_to_the_default(
        self,
    ) -> None:
        assert resolve_byok_provider_backend("glm", plan="enterprise") is None

    def test_a_plan_of_another_provider_resolves_to_nothing(self) -> None:
        assert resolve_byok_provider_backend("gemini", plan="coding_plan") is None
        assert resolve_byok_declared_plan("gemini", "coding_plan") is None

    def test_plan_match_is_trimmed_and_lowercased_like_the_provider(self) -> None:
        assert resolve_byok_provider_backend(
            " GLM ", plan=" General_API "
        ) == resolve_byok_provider_backend("glm", plan="general_api")

    def test_a_single_plan_provider_lists_its_one_plan(self) -> None:
        assert byok_provider_plans("openrouter") == ("free_tier",)
        assert byok_provider_plans("gemini") == ("ai_studio",)
        assert byok_provider_plans("nosuchprovider") == ()

    def test_the_general_api_row_is_the_pay_as_you_go_surface_by_declaration(
        self,
    ) -> None:
        general = resolve_byok_provider_backend("glm", plan="general_api")
        assert general is not None
        assert general.limit_model.billing == "pay_as_you_go"
        coding = resolve_byok_declared_plan("glm", "coding_plan")
        assert coding is not None
        assert coding.limit_model.billing == "flat_rate_quota"

    def test_the_retry_budget_is_declared_for_every_glm_plan_backend(self) -> None:
        assert byok_backend_max_retries("byok-glm") == 2
        assert byok_backend_max_retries("byok-glm-general") == 2
        assert byok_backend_max_retries("byok-gemini") == 2

    def test_the_general_row_has_no_house_rung_but_its_provider_is_house_keyed(
        self,
    ) -> None:
        """The general API is a customer-only surface: OMN-6790 forbids any house
        rung on it, so the plan has no bifrost mirror by design. What is still
        asserted is that the provider itself is handler-backed."""
        platform = _platform_backends()
        general = resolve_byok_provider_backend("glm", plan="general_api")
        assert general is not None
        assert not [
            b for b in platform if b.get("endpoint_url") == general.endpoint_url
        ]
        assert "glm" in house_keyed_provider_slugs(platform)


class TestEveryRowDeclaresALimitModel:
    def test_every_plan_row_carries_a_limit_model(self) -> None:
        rows = load_byok_plan_catalog()
        assert rows, "positive control: the plan catalogue must not be empty"
        for (provider, plan), row in rows.items():
            assert isinstance(row.limit_model, ModelByokLimitModel), (provider, plan)

    def test_a_flat_rate_or_free_row_declares_at_least_one_window(self) -> None:
        for (provider, plan), row in load_byok_plan_catalog().items():
            if row.limit_model.billing != "pay_as_you_go":
                assert row.limit_model.windows, (provider, plan)

    def test_the_coding_plan_declares_the_five_hour_and_weekly_credit_windows(
        self,
    ) -> None:
        coding = resolve_byok_declared_plan("glm", "coding_plan")
        assert coding is not None
        windows = {w.window_hours: w for w in coding.limit_model.windows}
        assert set(windows) == {5, 168}
        assert all(w.unit == "credits" for w in windows.values())
        assert windows[5].limit_by_tier == {"lite": 2000, "pro": 12000, "max": 28000}
        assert windows[168].limit_by_tier == {
            "lite": 10000,
            "pro": 60000,
            "max": 140000,
        }
        # The customer's tier is a fact about THEIR account, not the catalogue.
        assert windows[5].limit is None

    def test_gemini_counts_per_model_and_the_coding_plan_pools_per_plan(self) -> None:
        gemini = resolve_byok_provider_backend("gemini")
        coding = resolve_byok_declared_plan("glm", "coding_plan")
        assert gemini is not None
        assert coding is not None
        assert gemini.limit_model.counter_scope == "model"
        assert coding.limit_model.counter_scope == "plan"

    def test_every_window_names_its_source(self) -> None:
        for (provider, plan), row in load_byok_plan_catalog().items():
            for window in row.limit_model.windows:
                assert window.source.strip(), (provider, plan, window.window_hours)


class TestLimitCounterKey:
    """Per tenant + credential + provider + plan/model; the lab is a tenant."""

    def test_a_model_scoped_row_keys_on_the_model(self) -> None:
        row = resolve_byok_provider_backend("gemini")
        assert row is not None
        assert byok_limit_counter_key(
            "acme", "cred_acme_gemini_1", row, "gemini-3.5-flash-lite"
        ) == (
            "acme",
            "cred_acme_gemini_1",
            "gemini",
            "ai_studio",
            "gemini-3.5-flash-lite",
        )

    def test_a_plan_scoped_row_drops_the_model_so_models_pool_one_counter(
        self,
    ) -> None:
        row = resolve_byok_declared_plan("glm", "coding_plan")
        assert row is not None
        assert byok_limit_counter_key(
            "acme", "cred_acme_glm_1", row, "glm-5.3-flash"
        ) == (
            "acme",
            "cred_acme_glm_1",
            "glm",
            "coding_plan",
            None,
        )

    def test_two_tenants_and_two_credentials_never_share_a_counter(self) -> None:
        row = resolve_byok_provider_backend("gemini")
        assert row is not None
        keys = {
            byok_limit_counter_key("acme", "cred_acme_gemini_1", row, "m"),
            byok_limit_counter_key("acme", "cred_acme_gemini_2", row, "m"),
            byok_limit_counter_key("omninode", "cred_omninode_gemini_1", row, "m"),
        }
        assert len(keys) == 3

    def test_the_two_glm_plans_never_share_a_counter(self) -> None:
        coding = resolve_byok_declared_plan("glm", "coding_plan")
        general = resolve_byok_provider_backend("glm", plan="general_api")
        assert coding is not None
        assert general is not None
        assert byok_limit_counter_key(
            "t", "c", coding, "glm-5.3-flash"
        ) != byok_limit_counter_key("t", "c", general, "glm-5.3-flash")


class TestVertexStaysNotOffered:
    def test_vertex_is_not_offered(self) -> None:
        assert "vertex" not in customer_provider_catalogue()
        assert "vertex" in load_byok_not_offered_providers()

    def test_the_reason_states_the_scope_decision_and_not_a_gap(self) -> None:
        row = load_byok_not_offered_providers()["vertex"]
        reason = " ".join(row.reason.split()).lower()
        assert "sso" in reason
        assert "out of scope" in reason
        assert "access token" in reason
        # AC4: the reason cites the ruling, and the ticket that records it.
        assert "operator ruling 2026-09-30" in reason
        assert row.ticket == "OMN-20157"


class TestCatalogueShapeRefusals:
    def _providers(self) -> list[dict[str, Any]]:
        return _catalogue_payload()["providers"]

    def test_a_row_without_a_limit_model_is_refused(self, tmp_path: Path) -> None:
        payload = _catalogue_payload()
        del payload["providers"][0]["limit_model"]
        with pytest.raises(ByokCatalogError, match="limit_model"):
            mod._read_catalog(_write(tmp_path, payload))

    def test_a_row_without_a_plan_is_refused(self, tmp_path: Path) -> None:
        payload = _catalogue_payload()
        del payload["providers"][0]["plan"]
        with pytest.raises(ByokCatalogError, match="plan"):
            mod._read_catalog(_write(tmp_path, payload))

    def test_two_rows_for_one_provider_and_plan_are_refused(
        self, tmp_path: Path
    ) -> None:
        payload = _catalogue_payload()
        clone = dict(payload["providers"][0])
        clone["backend_id"] = "byok-clone"
        payload["providers"].append(clone)
        with pytest.raises(ByokCatalogError, match="more than once"):
            mod._read_catalog(_write(tmp_path, payload))

    def test_a_multi_plan_provider_needs_exactly_one_default_plan(
        self, tmp_path: Path
    ) -> None:
        payload = _catalogue_payload()
        for row in payload["providers"]:
            if row["provider"] == "glm":
                row["default_plan"] = False
        with pytest.raises(ByokCatalogError, match="default_plan"):
            mod._read_catalog(_write(tmp_path, payload))

    def test_two_default_plans_for_one_provider_are_refused(
        self, tmp_path: Path
    ) -> None:
        payload = _catalogue_payload()
        for row in payload["providers"]:
            if row["provider"] == "glm":
                row["default_plan"] = True
        with pytest.raises(ByokCatalogError, match="default_plan"):
            mod._read_catalog(_write(tmp_path, payload))

    def test_two_plans_may_not_share_a_backend_id(self, tmp_path: Path) -> None:
        payload = _catalogue_payload()
        glm_rows = [r for r in payload["providers"] if r["provider"] == "glm"]
        glm_rows[1]["backend_id"] = glm_rows[0]["backend_id"]
        with pytest.raises(ByokCatalogError, match="backend_id"):
            mod._read_catalog(_write(tmp_path, payload))

    def test_a_pay_as_you_go_free_of_windows_is_allowed_but_a_flat_rate_is_not(
        self, tmp_path: Path
    ) -> None:
        payload = _catalogue_payload()
        for row in payload["providers"]:
            if row["provider"] == "openrouter":
                row["limit_model"]["windows"] = []
        with pytest.raises(ByokCatalogError, match="window"):
            mod._read_catalog(_write(tmp_path, payload))

    def test_a_house_secret_ref_inside_the_limit_model_source_is_not_a_field(
        self, tmp_path: Path
    ) -> None:
        payload = _catalogue_payload()
        payload["providers"][0]["limit_model"]["secret_ref"] = "llm.openrouter.api_key"
        with pytest.raises(ByokCatalogError, match="secret_ref"):
            mod._read_catalog(_write(tmp_path, payload))
