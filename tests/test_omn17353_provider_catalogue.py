# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17353: the customer-facing provider catalogue is exactly the handler-backed set.

Launch rule (beta requirements r4 §2.4 / §2.5, axiom 2): local models by
default, bring-your-own key for any provider we ship a handler for, and Claude
is never a delegation target. Until this ticket nothing asserted the
customer-visible catalogue (``configs/byok_provider_backends.v1.yaml``, the
only authority that mints a BYOK route — OMN-17372) matched that shape in
EITHER direction.

The gate is bidirectional and declared, never inferred:

* every house-keyed cloud rung in ``bifrost_delegation.yaml`` (a backend that
  carries a ``secret_ref``) names a provider slug; that slug must be either
  OFFERED (a ``providers`` row) or DECLARED NOT OFFERED (a ``not_offered`` row
  carrying a reason and a ticket). A rung added without either fails here.
* every ``providers`` row and every ``not_offered`` row must be backed by a
  rung. A catalogue row addressing a provider the platform has no backend for
  fails here.
* no row in the file, offered or not, may name Claude/Anthropic.
* no row may carry a ``secret_ref`` — a house key on the customer path is
  impossible by construction, not by review.
* every offered provider id is accepted by the intake request model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from omnimarket.projection.credential_publisher import (
    ModelInferenceCredentialCreateRequest,
)
from omnimarket.routing import byok_provider_backends as mod
from omnimarket.routing.byok_provider_backends import (
    CATALOG_PATH,
    FORBIDDEN_PROVIDER_PATTERN,
    ByokCatalogError,
    ModelByokProviderBackend,
    catalogue_parity_gap,
    customer_provider_catalogue,
    house_keyed_provider_slugs,
    load_byok_not_offered_providers,
    load_byok_provider_catalog,
)

pytestmark = pytest.mark.unit

BIFROST_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)


def _platform_backends() -> list[dict[str, Any]]:
    payload = yaml.safe_load(BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))
    backends = [b for b in payload.get("backends", []) if isinstance(b, dict)]
    assert backends, "bifrost_delegation.yaml declared no backends"
    return backends


def _catalogue_text(**overrides: Any) -> str:
    """The live catalogue with top-level keys replaced — a mutation fixture."""
    payload = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    payload.update(overrides)
    return yaml.safe_dump(payload, sort_keys=False)


@pytest.fixture(autouse=True)
def _fresh_caches() -> Any:
    load_byok_provider_catalog.cache_clear()
    load_byok_not_offered_providers.cache_clear()
    yield
    load_byok_provider_catalog.cache_clear()
    load_byok_not_offered_providers.cache_clear()


class TestHandlerBackedSetDerivation:
    def test_house_keyed_slugs_come_from_the_secret_ref_not_the_backend_id(
        self,
    ) -> None:
        slugs = house_keyed_provider_slugs(
            [
                {"backend_id": "cloud-x", "secret_ref": "llm.openrouter.api_key"},
                {"backend_id": "local-coder"},  # keyless local rung: not BYOK-shaped
                {"backend_id": "cloud-y", "secret_ref": "llm.vertex.access_token"},
            ]
        )
        assert slugs == frozenset({"openrouter", "vertex"})

    def test_a_house_secret_ref_off_the_llm_slug_shape_fails_closed(self) -> None:
        with pytest.raises(ByokCatalogError, match=r"llm\.<provider>"):
            house_keyed_provider_slugs(
                [{"backend_id": "cloud-z", "secret_ref": "OPENROUTER_API_KEY"}]
            )

    def test_the_shipped_contract_yields_a_non_empty_house_keyed_set(self) -> None:
        # Positive control for every zero-gap assertion below.
        assert "openrouter" in house_keyed_provider_slugs(_platform_backends())


class TestCatalogueIsExactlyTheHandlerBackedSet:
    def test_every_house_keyed_rung_is_offered_or_declared_not_offered(
        self,
    ) -> None:
        gap = catalogue_parity_gap(
            house_keyed_provider_slugs(_platform_backends()),
            offered=customer_provider_catalogue(),
            not_offered=tuple(load_byok_not_offered_providers()),
        )
        assert gap.missing_from_catalogue == (), (
            "bifrost_delegation.yaml ships a house-keyed rung for "
            f"{gap.missing_from_catalogue} that the customer catalogue neither "
            "offers nor declares not-offered. Add a `providers` row (with a "
            "zero-cost default) or a `not_offered` row carrying a ticket."
        )
        assert gap.unbacked_in_catalogue == (), (
            f"{CATALOG_PATH.name} names {gap.unbacked_in_catalogue}, which no "
            "rung in bifrost_delegation.yaml backs; a catalogue row must never "
            "address a backend that does not exist."
        )

    def test_a_rung_added_without_a_catalogue_row_fails(self) -> None:
        # Negative control: the gate must see a new house-keyed rung.
        backends = [*_platform_backends(), {"secret_ref": "llm.newprov.api_key"}]
        gap = catalogue_parity_gap(
            house_keyed_provider_slugs(backends),
            offered=customer_provider_catalogue(),
            not_offered=tuple(load_byok_not_offered_providers()),
        )
        assert gap.missing_from_catalogue == ("newprov",)

    def test_a_catalogue_row_with_no_backend_fails(self) -> None:
        gap = catalogue_parity_gap(
            house_keyed_provider_slugs(_platform_backends()),
            offered=(*customer_provider_catalogue(), "nobackend"),
            not_offered=tuple(load_byok_not_offered_providers()),
        )
        assert gap.unbacked_in_catalogue == ("nobackend",)

    def test_offered_and_not_offered_are_disjoint_in_the_file(
        self, tmp_path: Path
    ) -> None:
        offered = next(iter(customer_provider_catalogue()))
        both = tmp_path / "both.yaml"
        both.write_text(
            _catalogue_text(
                not_offered=[
                    {"provider": offered, "reason": "x", "ticket": "OMN-17353"}
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(ByokCatalogError, match="both offered and not_offered"):
            mod._read_not_offered(both)

    def test_every_offered_provider_mirrors_a_rung_carrying_its_own_slug(
        self,
    ) -> None:
        """The endpoint/model the customer's key addresses is the rung whose
        house secret_ref names the same provider — the slug convention is
        asserted, not assumed."""
        backends = _platform_backends()
        for provider, backend in load_byok_provider_catalog().items():
            mirrored = [
                b
                for b in backends
                if b.get("endpoint_url") == backend.endpoint_url
                and b.get("model_name") == backend.model_name
            ]
            assert mirrored, f"{provider!r} mirrors no rung"
            assert set(house_keyed_provider_slugs(mirrored)) == {provider}, (
                f"{provider!r} mirrors rung(s) whose house secret_ref names a "
                "different provider"
            )

    def test_not_offered_rows_carry_a_ticket_each(self) -> None:
        rows = load_byok_not_offered_providers()
        assert rows, "expected at least one declared not-offered house-keyed provider"
        for provider, row in rows.items():
            assert re.fullmatch(r"OMN-\d+", row.ticket), (provider, row.ticket)
            assert row.reason.strip()

    def test_customer_provider_catalogue_is_sorted_non_empty_and_lowercase(
        self,
    ) -> None:
        catalogue = customer_provider_catalogue()
        assert catalogue
        assert list(catalogue) == sorted(catalogue)
        assert all(p == p.lower() for p in catalogue)
        assert "openrouter" in catalogue


class TestNoClaudeEntry:
    def test_the_forbidden_pattern_is_the_one_the_ticket_names(self) -> None:
        assert FORBIDDEN_PROVIDER_PATTERN.search("Claude")
        assert FORBIDDEN_PROVIDER_PATTERN.search("us.anthropic.opus")
        assert not FORBIDDEN_PROVIDER_PATTERN.search("openrouter")

    def test_no_offered_or_not_offered_row_names_claude(self) -> None:
        for provider in (
            *customer_provider_catalogue(),
            *load_byok_not_offered_providers(),
        ):
            assert not FORBIDDEN_PROVIDER_PATTERN.search(provider), provider

    def test_the_catalogue_refuses_a_claude_provider_row(self, tmp_path: Path) -> None:
        bad = tmp_path / "claude.yaml"
        bad.write_text(
            _catalogue_text(
                providers=[
                    {
                        "provider": "claude",
                        "backend_id": "byok-claude",
                        "endpoint_url": "https://example.invalid/v1/chat/completions",
                        "model_name": "x",
                    }
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(ByokCatalogError, match="never a delegation target"):
            mod._read_catalog(bad)

    def test_the_catalogue_refuses_a_claude_not_offered_row(
        self, tmp_path: Path
    ) -> None:
        bad = tmp_path / "claude_no.yaml"
        bad.write_text(
            _catalogue_text(
                not_offered=[
                    {"provider": "Anthropic", "reason": "x", "ticket": "OMN-17353"}
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(ByokCatalogError, match="never a delegation target"):
            mod._read_not_offered(bad)

    def test_no_platform_rung_is_a_claude_rung(self) -> None:
        # "not as a default, not as a fallback": the platform contract the
        # catalogue mirrors has no Anthropic surface either.
        for b in _platform_backends():
            for field in ("backend_id", "endpoint_url", "model_name", "secret_ref"):
                value = b.get(field)
                if isinstance(value, str):
                    assert not FORBIDDEN_PROVIDER_PATTERN.search(value), (
                        b.get("backend_id"),
                        field,
                        value,
                    )


class TestNoHouseCredentialByConstruction:
    def test_the_offered_row_model_has_no_secret_ref_field(self) -> None:
        assert "secret_ref" not in ModelByokProviderBackend.model_fields

    def test_the_catalogue_refuses_a_row_carrying_a_secret_ref(
        self, tmp_path: Path
    ) -> None:
        bad = tmp_path / "house.yaml"
        bad.write_text(
            _catalogue_text(
                providers=[
                    {
                        "provider": "openrouter",
                        "backend_id": "byok-openrouter",
                        "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
                        "model_name": "x",
                        "secret_ref": "llm.openrouter.api_key",
                    }
                ]
            ),
            encoding="utf-8",
        )
        with pytest.raises(ByokCatalogError, match="secret_ref"):
            mod._read_catalog(bad)


class TestIntakeAcceptsEveryOfferedProvider:
    def test_every_offered_provider_id_is_accepted_by_the_intake_model(
        self,
    ) -> None:
        for provider in customer_provider_catalogue():
            request = ModelInferenceCredentialCreateRequest(
                name="my-key",
                provider=provider,
                key_value=SecretStr("not-a-real-key"),
            )
            assert request.provider == provider
