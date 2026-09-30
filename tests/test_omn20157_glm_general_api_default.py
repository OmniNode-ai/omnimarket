# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20157: a customer's GLM key routes to the general API, never the Coding Plan.

z.ai's subscription terms (section 4, read 2026-09-30, quoted in
knowledge-base-internal ``reference/zai-glm-coding-plan-terms.md``) bar the GLM
Coding Plan quota from "directly invoking model APIs from your own applications,
bots, websites, SaaS products or other systems" and from letting "customers or
any organization" use it. A customer's Coding Plan key, sent to our route, is
exactly that, and the exposure lands on the customer's account first. So:

* the DEFAULT glm plan is ``general_api`` (``https://api.z.ai/api/paas/v4``);
* the ``coding_plan`` row stays in the catalogue as a detection-only surface:
  it is declared ``customer_routable: false`` and no route, overlay row or
  registration may address it;
* a key that answers only on the Coding Plan surface is refused with the typed
  ``BYOK_CODING_PLAN_NOT_PERMITTED``, which tells the customer to register a
  general API key. A key that answers on both surfaces is a general key.

Detection over a mock transport is pinned in ``tests/test_byok_plan_detection.py``;
the intake and CLI refusals are pinned here and in the CLI test directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.inference.local_byok_credential_adapter import (
    register_local_byok_credential,
)
from omnimarket.routing import byok_provider_backends as mod
from omnimarket.routing.byok_provider_backends import (
    CATALOG_PATH,
    ByokCatalogError,
    ByokPlanNotPermittedError,
    byok_provider_plans,
    byok_routable_plans,
    load_byok_not_offered_providers,
    load_byok_plan_catalog,
    load_byok_provider_catalog,
    require_byok_plan_permitted,
    resolve_byok_declared_plan,
    resolve_byok_provider_backend,
)

pytestmark = pytest.mark.unit

GENERAL_API_ENDPOINT = "https://api.z.ai/api/paas/v4/chat/completions"
CODING_PLAN_ENDPOINT = "https://api.z.ai/api/coding/paas/v4/chat/completions"
REFUSAL_CODE = "BYOK_CODING_PLAN_NOT_PERMITTED"
TERMS_REPORT = "reference/zai-glm-coding-plan-terms.md"


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


def _payload() -> dict[str, Any]:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "catalogue.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


class TestGeneralApiIsTheDefault:
    def test_a_glm_key_with_no_plan_resolves_the_general_api_route(self) -> None:
        default = resolve_byok_provider_backend("glm")
        assert default is not None
        assert default.plan == "general_api"
        assert default.backend_id == "byok-glm-general"
        assert default.endpoint_url == GENERAL_API_ENDPOINT
        assert load_byok_provider_catalog()["glm"] == default

    def test_the_general_api_is_the_only_routable_glm_plan(self) -> None:
        assert byok_routable_plans("glm") == ("general_api",)
        # The Coding Plan is still DECLARED, so detection can recognise its key.
        assert byok_provider_plans("glm") == ("coding_plan", "general_api")

    def test_the_coding_plan_never_resolves_a_customer_route(self) -> None:
        assert resolve_byok_provider_backend("glm", plan="coding_plan") is None

    def test_no_customer_routable_row_addresses_a_coding_plan_endpoint(self) -> None:
        rows = load_byok_plan_catalog().values()
        routable = [row for row in rows if row.customer_routable]
        assert routable, "positive control: some rows must be routable"
        for row in routable:
            assert "/coding/" not in row.endpoint_url, (row.provider, row.plan)

    def test_the_coding_plan_row_is_declared_detection_only_with_a_typed_refusal(
        self,
    ) -> None:
        row = resolve_byok_declared_plan("glm", "coding_plan")
        assert row is not None
        assert row.customer_routable is False
        assert row.default_plan is False
        assert row.endpoint_url == CODING_PLAN_ENDPOINT
        assert row.refusal_code == REFUSAL_CODE
        message = " ".join((row.refusal_message or "").split()).lower()
        assert "terms" in message
        assert "general api key" in message

    def test_a_declared_plan_lookup_does_not_widen_to_an_undeclared_one(self) -> None:
        assert resolve_byok_declared_plan("glm", "enterprise") is None
        assert resolve_byok_declared_plan("gemini", "coding_plan") is None

    def test_the_old_overlay_backend_id_still_reads_back_its_row(self) -> None:
        # A route minted before this change carries byok-glm; reading it back
        # describes it and does not route anything.
        old = mod.resolve_byok_backend_by_id("byok-glm")
        assert old is not None
        assert (old.provider, old.plan) == ("glm", "coding_plan")


class TestTheTypedRefusal:
    def test_a_coding_plan_registration_is_refused_with_the_typed_code(self) -> None:
        with pytest.raises(ByokPlanNotPermittedError) as excinfo:
            require_byok_plan_permitted("glm", "coding_plan")
        error = excinfo.value
        assert error.code == REFUSAL_CODE
        assert (error.provider, error.plan) == ("glm", "coding_plan")
        assert REFUSAL_CODE in str(error)
        assert "general API key" in str(error)

    def test_the_refusal_is_a_value_error_so_intake_maps_it_to_a_422(self) -> None:
        assert issubclass(ByokPlanNotPermittedError, ValueError)

    def test_a_routable_or_undeclared_plan_is_not_refused_here(self) -> None:
        require_byok_plan_permitted("glm", "general_api")
        require_byok_plan_permitted("gemini", "ai_studio")
        # An undeclared plan is the catalogue's other refusal, not this one.
        require_byok_plan_permitted("glm", "enterprise")


class TestTheLocalAdapterRefusesToo:
    def test_a_coding_plan_credential_is_never_written_locally(
        self, tmp_path: Path
    ) -> None:
        db = tmp_path / "delegation.sqlite"
        with pytest.raises(ByokPlanNotPermittedError) as excinfo:
            register_local_byok_credential(
                "glm", "customer-key-not-printed", plan="coding_plan", db_path=db
            )
        assert excinfo.value.code == REFUSAL_CODE
        assert not db.exists(), "nothing may be written for a refused plan"

    def test_a_general_api_credential_is_written(self, tmp_path: Path) -> None:
        db = tmp_path / "delegation.sqlite"
        ref = register_local_byok_credential(
            "glm", "customer-key-not-printed", plan="general_api", db_path=db
        )
        assert ref.startswith("cred_localinstall_glm_")


class TestTheCatalogueCitesItsReasons:
    def test_the_catalogue_cites_the_terms_report_and_keeps_the_invariants(
        self,
    ) -> None:
        text = CATALOG_PATH.read_text(encoding="utf-8")
        assert TERMS_REPORT in text
        assert "INV-068" in text
        assert "INV-098" in text


class TestCatalogueShapeRefusals:
    def _glm_rows(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [r for r in payload["providers"] if r["provider"] == "glm"]

    def test_the_shipped_catalogue_loads(self, tmp_path: Path) -> None:
        catalogue = mod._read_plan_catalog(_write(tmp_path, _payload()))
        assert ("glm", "coding_plan") in catalogue

    def test_a_detection_only_row_cannot_be_the_default_plan(
        self, tmp_path: Path
    ) -> None:
        payload = _payload()
        for row in self._glm_rows(payload):
            row["default_plan"] = row["plan"] == "coding_plan"
        with pytest.raises(ByokCatalogError, match="customer_routable"):
            mod._read_catalog(_write(tmp_path, payload))

    def test_a_detection_only_row_must_declare_its_refusal(
        self, tmp_path: Path
    ) -> None:
        payload = _payload()
        for row in self._glm_rows(payload):
            if row["plan"] == "coding_plan":
                del row["refusal_code"]
        with pytest.raises(ByokCatalogError, match="refusal_code"):
            mod._read_plan_catalog(_write(tmp_path, payload))

    def test_a_routable_row_may_not_declare_a_refusal(self, tmp_path: Path) -> None:
        payload = _payload()
        for row in self._glm_rows(payload):
            if row["plan"] == "general_api":
                row["refusal_code"] = REFUSAL_CODE
                row["refusal_message"] = "no"
        with pytest.raises(ByokCatalogError, match="refusal"):
            mod._read_plan_catalog(_write(tmp_path, payload))

    def test_a_provider_with_no_routable_plan_is_refused(self, tmp_path: Path) -> None:
        payload = _payload()
        payload["providers"] = [
            r
            for r in payload["providers"]
            if not (r["provider"] == "glm" and r["plan"] == "general_api")
        ]
        for row in self._glm_rows(payload):
            row["default_plan"] = False
        with pytest.raises(ByokCatalogError, match="customer_routable"):
            mod._read_catalog(_write(tmp_path, payload))
