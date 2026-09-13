# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18311: the customer provider catalogue holds no house-keyed provider.

Release criterion **C12**, third clause. OMN-17353 carries the first two
(catalogue parity both directions; no Claude entry) and is Done, so no carrier
could turn red on this one. The predicate, why the naive set intersection is
wrong, and what each conjunct protects are in the validator module's docstring —
this file asserts it and proves the assertion is falsifiable.

Every injection below is built from a REAL row of the shipped contracts, never a
synthetic one: a synthetic fixture proves the checker rejects a string it was
written to reject, not that it would catch the mistake a person would actually
make.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from omnimarket.routing.byok_provider_backends import (
    customer_provider_catalogue,
    house_keyed_provider_slugs,
    load_byok_not_offered_providers,
    load_byok_provider_catalog,
)
from omnimarket.validators import byok_catalogue_no_house_entry as mod
from omnimarket.validators.byok_catalogue_no_house_entry import (
    CUSTOMER_REGISTERABLE_SECRET_FIELDS,
    DECLARED_HOUSE_REF,
    HOUSE_RUNG_IDENTITY_COLLISION,
    NO_CUSTOMER_REGISTERABLE_KEY,
    HouseCatalogueError,
    find_house_keyed_catalogue_entries,
    format_findings,
    read_catalogue_rows,
    read_platform_rungs,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def rungs() -> list[dict[str, Any]]:
    return read_platform_rungs()


@pytest.fixture
def rows() -> list[dict[str, Any]]:
    return read_catalogue_rows()


def _house_rung(rungs: list[dict[str, Any]], field: str) -> dict[str, Any]:
    """A REAL platform rung whose house secret_ref ends in ``field``."""
    for rung in rungs:
        secret_ref = rung.get("secret_ref")
        if isinstance(secret_ref, str) and secret_ref.endswith(f".{field}"):
            return rung
    pytest.fail(f"bifrost_delegation.yaml ships no rung keyed by llm.<p>.{field}")


class TestTheShippedCatalogueIsClean:
    """GREEN half of AC2 — the clause holds on shipped state."""

    def test_the_shipped_catalogue_holds_no_house_keyed_row(
        self, rows: list[dict[str, Any]], rungs: list[dict[str, Any]]
    ) -> None:
        findings = find_house_keyed_catalogue_entries(rows, rungs)
        assert findings == (), format_findings(findings)

    def test_the_module_entry_point_the_hook_and_the_ci_job_run_exits_zero(
        self,
    ) -> None:
        assert mod.main() == 0

    def test_the_rows_checked_are_exactly_the_customer_facing_catalogue(
        self, rows: list[dict[str, Any]]
    ) -> None:
        """The gate must read the same rows the customer is offered.

        A gate that judged a different list than ``customer_provider_catalogue()``
        would pass while the customer-visible surface was dirty.
        """
        assert tuple(sorted(str(r["provider"]) for r in rows)) == (
            customer_provider_catalogue()
        )


class TestThePredicateIsNotTheNaiveIntersection:
    """AC1 — the openrouter/glm overlap is addressed, not ignored."""

    def test_the_naive_slug_intersection_is_non_empty_on_a_correct_catalogue(
        self, rungs: list[dict[str, Any]]
    ) -> None:
        """Pin the reason the obvious predicate is the wrong one.

        Being house-keyed on the platform rung is what makes a provider
        ELIGIBLE for BYOK — it proves we ship a handler and a credential path.
        If this ever became empty the catalogue would be offering providers the
        platform has no rung for, which is OMN-17353's failure, not a success
        here.
        """
        overlap = set(customer_provider_catalogue()) & house_keyed_provider_slugs(rungs)
        assert overlap == {"glm", "openrouter"}, overlap

    def test_every_offered_provider_is_house_keyed_on_the_platform_side(
        self, rungs: list[dict[str, Any]]
    ) -> None:
        house = house_keyed_provider_slugs(rungs)
        assert set(customer_provider_catalogue()) <= house


class TestRedOnAnInjectedHouseKeyedRow:
    """RED half of AC2 — the positive control, one per conjunct.

    Each injection is a real shipped row mutated with a real shipped value.
    """

    def test_conjunct_1_a_house_credential_ref_laundered_into_an_allowed_field(
        self, rows: list[dict[str, Any]], rungs: list[dict[str, Any]]
    ) -> None:
        real = _house_rung(rungs, "api_key")
        injected = copy.deepcopy(rows)
        # `extra="forbid"` already refuses an unknown `secret_ref` KEY at load
        # (OMN-17353). The reach this conjunct adds is the same house value on
        # a field the row is allowed to carry.
        injected[0]["model_name"] = str(real["secret_ref"])

        findings = find_house_keyed_catalogue_entries(injected, rungs)
        classes = {f.finding_class for f in findings}
        assert DECLARED_HOUSE_REF in classes, format_findings(findings)
        assert str(injected[0]["provider"]) in format_findings(findings)

    def test_conjunct_2_backend_id_collides_with_a_real_house_keyed_rung(
        self, rows: list[dict[str, Any]], rungs: list[dict[str, Any]]
    ) -> None:
        real = _house_rung(rungs, "api_key")
        injected = copy.deepcopy(rows)
        injected[0]["backend_id"] = str(real["backend_id"])

        findings = find_house_keyed_catalogue_entries(injected, rungs)
        collisions = [
            f for f in findings if f.finding_class == HOUSE_RUNG_IDENTITY_COLLISION
        ]
        assert collisions, format_findings(findings)
        assert collisions[0].provider == str(injected[0]["provider"])
        assert str(real["backend_id"]) in collisions[0].detail

    def test_conjunct_3_promoting_the_real_not_offered_vertex_row_fails(
        self, rows: list[dict[str, Any]], rungs: list[dict[str, Any]]
    ) -> None:
        """The exact mistake this conjunct exists for.

        ``vertex`` sits in ``not_offered`` because ``llm.vertex.access_token``
        is a platform-minted short-lived token no customer can register.
        Promoting it to ``providers:`` — a few lines of YAML — leaves every
        assertion in both existing suites passing, and the resulting catalogue
        row could only ever be served by the house credential.

        The injected row borrows the openrouter row's endpoint and model
        deliberately: the real vertex rung carries ``endpoint_url: null``, so an
        endpoint join finds nothing for it. The row still fails, on its SLUG,
        which is the relation conjunct 3 turns on.
        """
        declined = load_byok_not_offered_providers()
        assert "vertex" in declined, "shipped catalogue no longer declines vertex"
        rung = _house_rung(rungs, "access_token")
        assert str(rung["secret_ref"]).startswith("llm.vertex."), rung["secret_ref"]

        injected = copy.deepcopy(rows)
        injected.append(
            {
                "provider": "vertex",
                "backend_id": "byok-vertex",
                "endpoint_url": rows[0]["endpoint_url"],
                "model_name": rows[0]["model_name"],
                "max_retries": 2,
            }
        )

        findings = find_house_keyed_catalogue_entries(injected, rungs)
        unkeyable = [
            f for f in findings if f.finding_class == NO_CUSTOMER_REGISTERABLE_KEY
        ]
        assert unkeyable, format_findings(findings)
        assert unkeyable[0].provider == "vertex"
        assert "access_token" in unkeyable[0].detail
        assert "vertex" in format_findings(findings)

    def test_the_report_names_every_offending_slug_not_just_the_first(
        self, rows: list[dict[str, Any]], rungs: list[dict[str, Any]]
    ) -> None:
        injected = copy.deepcopy(rows)
        for row in injected:
            row["backend_id"] = str(_house_rung(rungs, "api_key")["backend_id"])
        report = format_findings(find_house_keyed_catalogue_entries(injected, rungs))
        for provider in customer_provider_catalogue():
            assert provider in report, (provider, report)


class TestFailsClosedRatherThanVacuously:
    """A zero that cannot be distinguished from an unrun check is not a pass."""

    def test_an_empty_platform_rung_set_is_refused_not_passed(
        self, rows: list[dict[str, Any]]
    ) -> None:
        with pytest.raises(HouseCatalogueError, match="vacuously"):
            find_house_keyed_catalogue_entries(rows, [])

    def test_a_row_no_house_rung_backs_is_reported_not_skipped(
        self, rows: list[dict[str, Any]], rungs: list[dict[str, Any]]
    ) -> None:
        injected = copy.deepcopy(rows)
        injected[0]["provider"] = "notarealprovider"
        findings = find_house_keyed_catalogue_entries(injected, rungs)
        assert any(f.finding_class == mod._UNBACKED for f in findings), format_findings(
            findings
        )

    def test_the_slug_join_finds_a_rung_an_endpoint_join_would_miss(
        self, rungs: list[dict[str, Any]]
    ) -> None:
        """Why conjunct 3 joins on the slug.

        The real vertex rung carries ``endpoint_url: null``, so an
        endpoint/model join finds nothing for it and a promoted vertex row would
        read as merely unevaluable instead of as the house entry it is.
        """
        rung = _house_rung(rungs, "access_token")
        assert rung.get("endpoint_url") is None, rung.get("endpoint_url")
        assert (
            mod.HOUSE_SECRET_REF_PATTERN.fullmatch(str(rung["secret_ref"])) is not None
        )

    def test_an_absent_catalogue_raises_rather_than_returning_no_rows(
        self, tmp_path: Any
    ) -> None:
        with pytest.raises(HouseCatalogueError, match="not found"):
            read_catalogue_rows(tmp_path / "absent.yaml")

    def test_an_absent_platform_contract_raises(self, tmp_path: Any) -> None:
        with pytest.raises(HouseCatalogueError, match="not found"):
            read_platform_rungs(tmp_path / "absent.yaml")

    def test_the_raw_rows_are_read_unvalidated_so_conjunct_1_is_falsifiable(
        self, rows: list[dict[str, Any]]
    ) -> None:
        """Reading through ``ModelByokProviderBackend`` would make conjunct 1
        unfalsifiable: ``extra="forbid"`` refuses the row before the check runs,
        so the check would pass for the wrong reason."""
        assert rows
        assert all(isinstance(row, dict) for row in rows)
        loaded = load_byok_provider_catalog()
        assert "secret_ref" not in type(next(iter(loaded.values()))).model_fields


class TestTheRegisterableFieldSetIsDeclaredNotInferred:
    def test_only_api_key_is_customer_registerable_today(self) -> None:
        assert frozenset({"api_key"}) == CUSTOMER_REGISTERABLE_SECRET_FIELDS

    def test_every_shipped_house_field_is_either_registerable_or_declined(
        self, rungs: list[dict[str, Any]]
    ) -> None:
        """A house credential field that is neither registerable nor attached to
        a declined provider is a provider we could offer and have not judged."""
        declined = set(load_byok_not_offered_providers())
        offered = set(customer_provider_catalogue())
        for rung in rungs:
            secret_ref = rung.get("secret_ref")
            if not isinstance(secret_ref, str):
                continue
            match = mod.HOUSE_SECRET_REF_PATTERN.fullmatch(secret_ref)
            if match is None:
                continue
            slug, field = match.group("slug"), match.group("field")
            if field in CUSTOMER_REGISTERABLE_SECRET_FIELDS:
                continue
            assert slug in declined or slug not in offered, (
                f"{slug!r} is offered but its house credential {secret_ref!r} is "
                f"a {field!r} no customer can register"
            )
