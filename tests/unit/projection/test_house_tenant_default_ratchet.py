# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ratchet pinning the house-tenant DEFAULT-allowed state and its flip condition.

OPERATOR RULING 2026-08-02 (house tenant), verbatim intent: every
workload-attributable row is tenant data; OmniNode itself is a first-class
tenant with its own fixed tenant id; and **writers default to the house tenant
ONLY until customer ingress exists, then missing attribution fails closed.**

The ruling's own words for the second half were "an explicit ratchet with a
named flip condition, not a comment". A comment is what this file replaces. It
does three things a comment cannot:

1. **Pins the default-allowed state.** Today a projection writer that resolves
   no tenant OMITS ``tenant_id`` and Postgres' ``DEFAULT 'omninode'`` supplies
   the house tenant. That is deliberate and operator-accepted (OMN-14058), not
   drift. :func:`test_the_omit_path_is_currently_allowed` asserts it, so a
   change to the writer boundary cannot happen silently.

2. **Names the flip condition mechanically.** The flip condition is *customer
   ingress existing*. "Customer ingress" is not a vibe: concretely it is a
   production code path that mints a verified tenant capability and binds it,
   i.e. a non-test caller of
   ``omnibase_infra.runtime.dispatch_envelope_context.bind_projection_tenant_authority``
   (minted only by ``verify_signed_projection_tenant_authority`` from a signed
   ``ModelMessageEnvelope``). As of the pinned ``omnibase-infra`` revision that
   function has **zero** non-test call sites in the shipped package: the whole
   tenant-authority path is built and unwired, so
   ``current_projection_tenant_authority()`` returns ``None`` on every real
   dispatch. :func:`test_customer_ingress_does_not_exist_yet` measures that
   directly against the INSTALLED package -- not a flag, not a docstring -- and
   **fails the moment the first production ingress appears.**

3. **Says what to do when it fires.** See the failure message on that test.

WHAT A FAILURE HERE MEANS
-------------------------
:func:`test_customer_ingress_does_not_exist_yet` going RED is not a bug in this
test. It is the ratchet firing: customer ingress now exists, so the interim is
over. The required follow-through, in order, is:

  a. flip ``ENFORCE_TENANT_ISOLATION`` to ``true`` so
     :func:`~omnimarket.projection.tenant_isolation.require_tenant_id` refuses
     an unattributed write instead of letting the column DEFAULT absorb it;
  b. re-key the uniqueness on the tenant-classified relations to include
     ``tenant_id`` (OMN-15356 / OMN-14894) so two tenants writing the same
     natural key coexist instead of clobbering each other -- this is a
     prerequisite, not a cleanup;
  c. convert the ``TEXT`` slug columns to the canonical UUID in ONE pass
     (OMN-15356), resolving the house tenant to
     :data:`~omnimarket.projection.tenant_isolation.HOUSE_TENANT_UUID`;
  d. delete this file.

References: OMN-15655 (this classification), OMN-15423 (relation inventory),
OMN-14058 (the operator-accepted single-tenant interim), OMN-14894 / OMN-15356
(tenant RLS + canonical UUID), OMN-14899 (the non-superuser reader role),
OMN-15359 (the physical schema cutover), ADR-0027.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import omnibase_infra
import pytest

from omnimarket.config.settings import get_settings
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    TenantRequiredError,
    house_tenant_write_stamp,
    require_tenant_id,
)

_INGRESS_BINDER: Final[str] = "bind_projection_tenant_authority"
_INFRA_ROOT: Final[Path] = Path(omnibase_infra.__file__).resolve().parent

# The module that DEFINES the binder plus its own ``__all__`` re-export. A
# definition is not an ingress; only a caller is.
_DEFINITION_SITES: Final[frozenset[str]] = frozenset(
    {"runtime/dispatch_envelope_context.py"}
)


def _production_ingress_call_sites() -> tuple[str, ...]:
    """Every non-test call of the tenant-authority binder in shipped infra.

    Parses the installed package rather than grepping strings, so a mention in
    a docstring, comment or ``__all__`` list does not count as an ingress.
    """
    sites: list[str] = []
    for path in sorted(_INFRA_ROOT.rglob("*.py")):
        relative = path.relative_to(_INFRA_ROOT).as_posix()
        if relative in _DEFINITION_SITES:
            continue
        source = path.read_text(encoding="utf-8")
        if _INGRESS_BINDER not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - shipped package must parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == _INGRESS_BINDER:
                sites.append(f"{relative}:{node.lineno}")
    return tuple(sites)


def test_customer_ingress_does_not_exist_yet() -> None:
    """The named flip condition, measured against the installed package.

    RED here == customer ingress now exists == the house-tenant default is no
    longer the correct writer behaviour. Follow the ordered steps in this
    module's docstring; do not silence this test.
    """
    sites = _production_ingress_call_sites()
    assert not sites, (
        "RATCHET FIRED -- customer ingress now EXISTS. "
        f"{_INGRESS_BINDER}() is called from production omnibase_infra code at "
        f"{list(sites)}, so a real per-request tenant capability now reaches "
        "the projection write path. The house-tenant DEFAULT is no longer the "
        "right behaviour for an unattributed write: it must fail closed "
        "instead. Do NOT delete or xfail this assertion -- work the ordered "
        "steps in this module's docstring (enforce, re-key uniqueness, convert "
        "to the canonical UUID), then delete this file."
    )


def test_the_omit_path_is_currently_allowed() -> None:
    """Pin the interim: no configured tenant -> omit the key, do not raise."""
    settings = get_settings()
    assert settings.onex_tenant_id.strip() == "", (
        "this ratchet measures the unattributed path; a lane that configures "
        "ONEX_TENANT_ID is not the interim state this test pins"
    )
    assert settings.enforce_tenant_isolation is False, (
        "ENFORCE_TENANT_ISOLATION is on, so the interim default-allowed state "
        "this test pins no longer holds -- see this module's docstring"
    )
    assert house_tenant_write_stamp(table="capability_scores") == {}, (
        "the writer must OMIT tenant_id so the column DEFAULT supplies the "
        "house tenant; writing the key as NULL is the OMN-14058 erasure shape"
    )
    require_tenant_id(None, table="capability_scores")


def test_enforcement_makes_the_omit_path_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the ratchet: with enforcement on, the write refuses.

    Proves the flip is wired, not aspirational -- the guard the eight
    newly-classified writers call raises BEFORE any row is built, so a refused
    write produces zero rows.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "enforce_tenant_isolation", True, raising=True)
    with pytest.raises(TenantRequiredError):
        house_tenant_write_stamp(table="capability_scores")


def test_a_configured_tenant_is_stamped_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane that configures a tenant records it on the row, not the default.

    The GUC an RLS policy compares against must equal what the database
    actually stored (proven by execution in OMN-15301), so a configured tenant
    has to reach the row rather than being silently absorbed by the house
    default -- the OMN-14485 live-no-op shape.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "onex_tenant_id", "  acme  ", raising=True)
    assert house_tenant_write_stamp(table="capability_scores") == {"tenant_id": "acme"}
    assert HOUSE_TENANT_SLUG not in ("acme",)
