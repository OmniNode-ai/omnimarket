# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a customer catalogue row that resolves to a HOUSE credential.

OMN-18311, release criterion **C12** third clause: *"Provider catalogue equals
the handler-backed set; no Claude entry; **no house entry**."* The first two
clauses are carried by OMN-17353 and asserted by
``tests/test_omn17353_provider_catalogue.py``. The third was carried by nothing:
none of OMN-17353's four acceptance items states a no-house-entry condition, and
because that ticket is Done no carrier could ever turn red on it again.

What a "house-keyed entry" is, and why the naive predicate is wrong
------------------------------------------------------------------
``house_keyed_provider_slugs()`` derives provider slugs from the house
``secret_ref`` on a **platform rung**. On the shipped catalogue those two sets
overlap by name: ``openrouter`` and ``glm`` are house-keyed on the platform side
*and* legitimately customer-offered. So

    set(customer_provider_catalogue()) & house_keyed_provider_slugs(rungs)

is ``{"glm", "openrouter"}`` on a CORRECT catalogue. The naive "that
intersection is empty" reading would fail RED on shipped state, and asserting it
would be asserting the opposite of the launch rule: a provider slug being
house-keyed on the platform rung is precisely what makes it *eligible* for BYOK,
because it proves we ship a handler and a credential path for it.

The clause is not about the SLUG. It is about the CREDENTIAL a customer's
request resolves to. A house entry is a catalogue row that, when a customer
request uses it, is answered on OUR key rather than theirs — a row that looks
identical to a bring-your-own-key row on the catalogue and bills to us.

The predicate, as three conjuncts over the shipped state
-------------------------------------------------------
For every row ``R`` in ``configs/byok_provider_backends.v1.yaml`` ``providers:``
— the rows :func:`~omnimarket.routing.byok_provider_backends.customer_provider_catalogue`
enumerates and :func:`~omnimarket.routing.byok_provider_backends.resolve_byok_provider_backend`
returns — against the platform rungs in ``configs/bifrost_delegation.yaml``:

1. **No house credential reference on the row.** No field VALUE on ``R``
   matches the house ``llm.<provider>.<field>`` shape. Not just the ``secret_ref``
   key: any field. ``extra="forbid"`` on ``ModelByokProviderBackend`` already
   refuses an unknown ``secret_ref`` key at load (OMN-17353,
   ``TestNoHouseCredentialByConstruction``), so this conjunct's new reach is a
   house ref laundered into an ALLOWED field.
2. **No house rung identity collision.** ``R.backend_id`` is not the
   ``backend_id`` of any platform rung that carries a house ``secret_ref``. This
   is the sharpest vector: a tenant-overlay decision derives
   ``selected_backend_ref`` from ``backend_id``
   (``_decision_from_tenant_overlay``) and :func:`byok_backend_max_retries`
   keys on it, so a colliding id both attributes a customer-paid call to a house
   rung and points the resolution at one. Asserted today only by
   ``tests/test_omn17372_byok_routing_overlay_bridge.py::...::test_byok_backend_ids_never_collide_with_platform_rung_names``,
   a routing-bridge suite that does not name this clause; it is brought under
   the clause here so C12 has a carrier that can turn red.
3. **The customer can actually key it.** At least one platform rung whose
   house ``secret_ref`` names ``R``'s provider slug must carry a FIELD segment
   that is customer-registerable — an API key
   (:data:`CUSTOMER_REGISTERABLE_SECRET_FIELDS`), not a platform-minted
   short-lived token. This is asserted NOWHERE today and is the reason
   ``vertex`` sits in ``not_offered``: its house credential is
   ``llm.vertex.access_token``, which no customer can register. Nothing stops
   that row being moved to ``providers:`` — every existing assertion in both
   suites would still pass, and the resulting catalogue row could only ever be
   served by the platform's own token. That is a house entry.

   The join is on the SLUG, not on ``endpoint_url``/``model_name``. A rung the
   fleet binds per lane carries ``endpoint_url: null`` in the committed contract
   (``cloud-vertex-gemini`` does), so an endpoint join silently fails to find
   exactly the rungs this conjunct most needs to judge. OMN-17353's
   ``test_every_offered_provider_mirrors_a_rung_carrying_its_own_slug`` already
   pins that the two joins agree for the rows that have a concrete endpoint.

A row whose slug no house rung names cannot be evaluated for conjunct 3, so it
is reported rather than silently passed (:data:`_UNBACKED`) — an unevaluable row
and a clean row must not read the same. The primary both-directions parity gate
is OMN-17353's, not this one; this is its fail-closed edge.

Run standalone (the ``byok-catalogue-no-house-entry`` pre-commit hook and the
``BYOK Catalogue No House Entry`` CI job both call this entry point)::

    uv run python -m omnimarket.validators.byok_catalogue_no_house_entry
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field

_CONFIGS_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "configs"

#: The customer-facing catalogue this validator guards.
CATALOGUE_PATH: Final[Path] = _CONFIGS_DIR / "byok_provider_backends.v1.yaml"

#: The platform contract the house-keyed rung set is derived from.
BIFROST_CONTRACT_PATH: Final[Path] = _CONFIGS_DIR / "bifrost_delegation.yaml"

#: A HOUSE credential reference: ``llm.<provider>.<field>``. Same shape
#: ``house_keyed_provider_slugs`` derives slugs from, widened here to capture
#: the FIELD segment too — conjunct 3 turns on which field it is.
HOUSE_SECRET_REF_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^llm\.(?P<slug>[a-z0-9_-]+)\.(?P<field>[a-z0-9_]+)$"
)

#: House ``secret_ref`` field segments a CUSTOMER can supply a value for. An
#: API key is registerable: the customer pastes it into
#: ``POST /v1/tenants/me/inference-credentials`` and it is minted as a
#: tenant-scoped ref. A short-lived platform token (``llm.vertex.access_token``)
#: is not — it is issued to US, by our identity, and no customer submission can
#: stand in for it. Offering such a provider puts a row on the catalogue that
#: only the house credential can serve.
CUSTOMER_REGISTERABLE_SECRET_FIELDS: Final[frozenset[str]] = frozenset({"api_key"})

DECLARED_HOUSE_REF: Final[str] = "declared_house_ref"
HOUSE_RUNG_IDENTITY_COLLISION: Final[str] = "house_rung_identity_collision"
NO_CUSTOMER_REGISTERABLE_KEY: Final[str] = "no_customer_registerable_key"
_UNBACKED: Final[str] = "unbacked_row_not_evaluable"


class HouseCatalogueError(ValueError):
    """The catalogue or the platform contract could not be read to judge it.

    Raised rather than returning "no findings": an unreadable input and a clean
    catalogue must never produce the same verdict.
    """


class ModelHouseEntryFinding(BaseModel):
    """One customer catalogue row that resolves to a house credential."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    finding_class: str = Field(min_length=1)
    detail: str = Field(min_length=1)


_REMEDY: Final[dict[str, str]] = {
    DECLARED_HOUSE_REF: (
        "a customer catalogue row carries a house llm.<provider>.<field> "
        "reference. The customer's own minted ref is supplied per registration "
        "by the projection writer and is never read from this file — remove the "
        "value."
    ),
    HOUSE_RUNG_IDENTITY_COLLISION: (
        "a customer catalogue row's backend_id is a house-keyed platform rung's "
        "backend_id. A tenant-overlay decision derives selected_backend_ref from "
        "it, so the customer's call both resolves against and is billed to the "
        "house rung. Namespace the id (byok-<provider>)."
    ),
    NO_CUSTOMER_REGISTERABLE_KEY: (
        "every platform rung carrying this row's provider slug authenticates "
        "with a credential no customer can register, so only the house "
        "credential can ever serve the row. Move the provider to not_offered "
        "with the ticket that owns lifting "
        "it, or add the field to CUSTOMER_REGISTERABLE_SECRET_FIELDS once a "
        "customer can genuinely supply one."
    ),
    _UNBACKED: (
        "no platform rung carries a house secret_ref naming this row's provider "
        "slug, so whether a customer can key it cannot be judged. Back the row "
        "with a rung (the both-directions parity gate is OMN-17353's)."
    ),
}


def _load_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise HouseCatalogueError(f"required contract not found at {path}")
    payload: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise HouseCatalogueError(f"contract at {path} is not a mapping")
    return payload


def read_catalogue_rows(path: Path = CATALOGUE_PATH) -> list[dict[str, Any]]:
    """The RAW ``providers:`` rows, unvalidated.

    Raw on purpose: the loaded :class:`ModelByokProviderBackend` cannot express
    a house ``secret_ref`` at all, so reading through it would make conjunct 1
    unfalsifiable — the check would pass because the shape refused the row
    earlier, not because the row was clean.
    """
    rows = _load_mapping(path).get("providers")
    if not isinstance(rows, list) or not rows:
        raise HouseCatalogueError(f"catalogue at {path} declares no providers list")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise HouseCatalogueError(f"catalogue at {path} has a non-mapping row")
        out.append(dict(row))
    return out


def read_platform_rungs(path: Path = BIFROST_CONTRACT_PATH) -> list[dict[str, Any]]:
    """The RAW ``backends:`` rungs of the platform delegation contract."""
    rungs = _load_mapping(path).get("backends")
    if not isinstance(rungs, list) or not rungs:
        raise HouseCatalogueError(f"platform contract at {path} declares no backends")
    return [dict(r) for r in rungs if isinstance(r, Mapping)]


def _house_refs(rung: Mapping[str, Any]) -> re.Match[str] | None:
    secret_ref = rung.get("secret_ref")
    if secret_ref is None:
        return None
    return HOUSE_SECRET_REF_PATTERN.fullmatch(str(secret_ref))


def find_house_keyed_catalogue_entries(
    catalogue_rows: Iterable[Mapping[str, Any]],
    platform_rungs: Iterable[Mapping[str, Any]],
) -> tuple[ModelHouseEntryFinding, ...]:
    """Evaluate the three conjuncts. Pure: no file or network read.

    Returns every violated conjunct for every row, not the first — a row can be
    house-keyed in more than one way and truncating to the first hides the rest
    behind a fix for the one reported.
    """
    rungs = list(platform_rungs)
    if not rungs:
        raise HouseCatalogueError(
            "no platform rungs supplied; the house-keyed set would be empty and "
            "every conjunct would pass vacuously"
        )

    house_rung_ids: dict[str, str] = {}
    for rung in rungs:
        match = _house_refs(rung)
        backend_id = rung.get("backend_id")
        if match is not None and isinstance(backend_id, str):
            house_rung_ids[backend_id] = str(rung.get("secret_ref"))

    findings: list[ModelHouseEntryFinding] = []
    for row in catalogue_rows:
        provider = str(row.get("provider", "")).strip() or "<unnamed row>"

        # Conjunct 1 — no house credential reference in ANY field value.
        for key, value in sorted(row.items()):
            if not isinstance(value, str):
                continue
            if HOUSE_SECRET_REF_PATTERN.fullmatch(value):
                findings.append(
                    ModelHouseEntryFinding(
                        provider=provider,
                        finding_class=DECLARED_HOUSE_REF,
                        detail=(
                            f"field {key!r} carries house credential reference "
                            f"{value!r}"
                        ),
                    )
                )

        # Conjunct 2 — no house rung identity collision.
        backend_id = row.get("backend_id")
        if isinstance(backend_id, str) and backend_id in house_rung_ids:
            findings.append(
                ModelHouseEntryFinding(
                    provider=provider,
                    finding_class=HOUSE_RUNG_IDENTITY_COLLISION,
                    detail=(
                        f"backend_id {backend_id!r} is a platform rung keyed by "
                        f"{house_rung_ids[backend_id]!r}"
                    ),
                )
            )

        # Conjunct 3 — some house rung for this slug is customer-registerable.
        backing = [
            (rung, match)
            for rung in rungs
            if (match := _house_refs(rung)) is not None
            and match.group("slug") == provider
        ]
        if not backing:
            findings.append(
                ModelHouseEntryFinding(
                    provider=provider,
                    finding_class=_UNBACKED,
                    detail=(
                        "no platform rung carries a house secret_ref naming "
                        f"slug {provider!r}"
                    ),
                )
            )
            continue
        if not any(
            match.group("field") in CUSTOMER_REGISTERABLE_SECRET_FIELDS
            for _, match in backing
        ):
            fields = sorted({match.group("field") for _, match in backing})
            ids = sorted({str(rung.get("backend_id")) for rung, _ in backing})
            findings.append(
                ModelHouseEntryFinding(
                    provider=provider,
                    finding_class=NO_CUSTOMER_REGISTERABLE_KEY,
                    detail=(
                        f"every house rung for {provider!r} ({', '.join(ids)}) "
                        f"authenticates with {', '.join(fields)} — none is a "
                        "customer-registerable key"
                    ),
                )
            )
    return tuple(findings)


def format_findings(findings: Sequence[ModelHouseEntryFinding]) -> str:
    """Render findings as an operator-readable report naming every slug."""
    slugs = sorted({f.provider for f in findings})
    lines = [
        "customer provider catalogue holds house-keyed row(s) (OMN-18311, C12):",
        f"  offending provider(s): {', '.join(slugs)}",
        "",
    ]
    for finding in sorted(
        findings, key=lambda f: (f.provider, f.finding_class, f.detail)
    ):
        lines.append(
            f"  [{finding.finding_class}] {finding.provider}: {finding.detail}"
        )
        lines.append(f"      {_REMEDY[finding.finding_class]}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    findings = find_house_keyed_catalogue_entries(
        read_catalogue_rows(), read_platform_rungs()
    )
    if not findings:
        return 0
    sys.stderr.write(format_findings(findings))
    return 1


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
