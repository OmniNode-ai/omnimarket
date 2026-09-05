# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider→backend catalog for customer-supplied (BYOK) inference keys.

Loads ``configs/byok_provider_backends.v1.yaml`` — the declared answer to the
one question the BYOK routing bridge has to ask: *a customer registered a key
for provider ``X``; which backend does a delegation on that key address?*

Why this is a catalog lookup and not a derivation
-------------------------------------------------
The platform's own OpenRouter rungs in ``bifrost_delegation.yaml`` each carry
``secret_ref: llm.openrouter.api_key`` — a HOUSE credential. Deriving a
customer's route by "reuse the platform backend whose endpoint host matches
the provider" would inherit that ref by construction, and answering a customer
on a house credential is precisely what OMN-17372 ruling 3 forbids. So the
BYOK binding is declared separately and the ``secret_ref`` is supplied by the
caller from the tenant's OWN minted ref, never read from this file. This
module has no ``secret_ref`` field at all — the omission is the mechanism.

Fail-closed
-----------
:func:`resolve_byok_provider_backend` returns ``None`` for any provider the
catalog does not declare. ``None`` means *no route is minted* — the credential
is still catalogued (the customer can see they registered it), but no
``delegation_routing_tenant_overlay`` row exists, so nothing selects it. It
must never fall back to a platform backend.

Related:
    - OMN-17372: cloud delegation on a customer's OpenRouter key (blocker b3)
    - OMN-15631: the ``delegation_routing_tenant_overlay`` table + resolver
    - OMN-17373: ``openai`` is deliberately absent — it has no backend yet
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: Schema tag the catalog file must declare. A file that does not carry it is
#: refused rather than partially read — a silently-mis-shaped routing catalog
#: is worse than an absent one.
BYOK_CATALOG_SCHEMA_VERSION = "byok_provider_backends.v1"

CATALOG_PATH: Path = (
    Path(__file__).parent.parent / "configs" / "byok_provider_backends.v1.yaml"
)


class ByokCatalogError(ValueError):
    """The BYOK provider catalog is absent or mis-shaped.

    Raised rather than degraded-to-empty: an empty catalog and a broken
    catalog are indistinguishable to the caller, and the second one silently
    stops minting routes for every customer at once.
    """


class ModelByokProviderBackend(BaseModel):
    """One declared BYOK backend binding, keyed by the customer's provider id.

    A 1:1 source for the writable columns of a
    ``delegation_routing_tenant_overlay`` row EXCEPT ``tenant_id``,
    ``task_type`` and ``secret_ref``, which are per-registration facts the
    caller supplies. There is deliberately no ``secret_ref`` here — see the
    module docstring.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    endpoint_url: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    timeout_ms: int | None = Field(default=None, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)


def _read_catalog(path: Path) -> dict[str, ModelByokProviderBackend]:
    if not path.is_file():
        raise ByokCatalogError(
            f"BYOK provider catalog not found at {path}. Every customer-supplied "
            "inference key resolves its route through this file; without it no "
            "BYOK delegation can be routed at all."
        )
    with open(path) as handle:
        raw: Any = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ByokCatalogError(f"BYOK provider catalog at {path} is not a mapping.")

    declared_version = raw.get("schema_version")
    if declared_version != BYOK_CATALOG_SCHEMA_VERSION:
        raise ByokCatalogError(
            f"BYOK provider catalog at {path} declares schema_version "
            f"{declared_version!r}; expected {BYOK_CATALOG_SCHEMA_VERSION!r}."
        )

    entries = raw.get("providers")
    if not isinstance(entries, list):
        raise ByokCatalogError(
            f"BYOK provider catalog at {path} has no 'providers' list."
        )

    catalog: dict[str, ModelByokProviderBackend] = {}
    for entry in entries:
        backend = ModelByokProviderBackend.model_validate(entry)
        if backend.provider in catalog:
            raise ByokCatalogError(
                f"BYOK provider catalog at {path} declares provider "
                f"{backend.provider!r} more than once; one provider must resolve "
                "to exactly one backend."
            )
        catalog[backend.provider] = backend
    return catalog


@lru_cache(maxsize=1)
def load_byok_provider_catalog() -> dict[str, ModelByokProviderBackend]:
    """Load and cache the declared BYOK provider→backend catalog.

    Cached for the process lifetime: the catalog ships inside the wheel
    (``[tool.hatch.build] artifacts`` packages ``src/omnimarket/**/*.yaml``)
    and cannot change under a running consumer. Call
    ``load_byok_provider_catalog.cache_clear()`` in tests that rewrite it.

    Raises:
        ByokCatalogError: the file is absent, is not a mapping, declares the
            wrong ``schema_version``, has no ``providers`` list, or declares
            one provider twice.
    """
    return _read_catalog(CATALOG_PATH)


def resolve_byok_provider_backend(provider: str) -> ModelByokProviderBackend | None:
    """Resolve the declared BYOK backend for ``provider``, or ``None``.

    ``None`` is the fail-CLOSED answer for an undeclared provider: the caller
    mints no routing overlay row, so a delegation for that tenant selects
    nothing rather than inheriting a platform backend and its house
    credential.

    Matching is exact on the provider string the customer submitted, lowercased
    and stripped. ``ModelInferenceCredentialCreateRequest.provider`` already
    constrains that string to ``^[A-Za-z0-9_-]+$``, so case is the only
    normalisation a legitimate submission can need.
    """
    normalized = provider.strip().lower()
    if not normalized:
        return None
    return load_byok_provider_catalog().get(normalized)


__all__: list[str] = [
    "BYOK_CATALOG_SCHEMA_VERSION",
    "CATALOG_PATH",
    "ByokCatalogError",
    "ModelByokProviderBackend",
    "load_byok_provider_catalog",
    "resolve_byok_provider_backend",
]
