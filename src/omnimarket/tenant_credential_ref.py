# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The single authority for the BYOK credential-ref shape (OMN-16944).

A tenant's inference credential is registered through
``POST /v1/tenants/me/inference-credentials``. Intake mints an opaque ref
(``cred_{tenant_id}_{provider}_{uuid4hex}``), writes the VALUE into the secret
store under that name, and publishes only the ref.

Two independent surfaces have to agree on that shape:

* the **producer** -- ``projection.credential_publisher.mint_api_key_ref``;
* the **consumer** -- the effect boundary, which must recognise a tenant ref in
  order to refuse it the house-key fallback path
  (``inference.secret_store_resolver``), and the lane's declared secret
  namespace rule, which must claim exactly the refs the minter emits.

They live in one module so they cannot drift apart, and
``tests/test_omn16944_tenant_ref_fail_closed.py`` asserts that minted output is
recognised by both the matcher and the lane pattern.

The ref carries no secret material: it is safe to log, publish and display.

Matching is deliberately PERMISSIVE on the middle of the ref and strict on its
ends. ``tenant_id`` comes from the authenticated principal and ``provider`` is
constrained only to ``[A-Za-z0-9_-]+`` by
``ModelInferenceCredentialCreateRequest`` -- both may contain ``_``, so a
segment-by-segment pattern would fail to match some legitimately-minted refs.
Failing to match is the DANGEROUS direction here: an unrecognised tenant ref
falls back to the generic resolution path that accepts a house ``api_key_env``.
Matching a superset costs nothing (the only consequence of being claimed is
losing that fallback), so the pattern anchors on what is structural and
un-spoofable -- the ``cred_`` prefix and the 32-hex uuid4 suffix.
"""

from __future__ import annotations

import re

# The lane-declarable form of the same shape. Declared verbatim as a
# ``ModelSecretNamespaceRule.ref_pattern`` (omnibase_infra, OMN-16944) so the
# deployed resolver claims exactly the refs this module recognises.
TENANT_CREDENTIAL_LANE_REF_PATTERN = r"^cred_\S+_[0-9a-f]{32}$"

_TENANT_CREDENTIAL_REF = re.compile(TENANT_CREDENTIAL_LANE_REF_PATTERN)

# Splits a matched ref into "<tenant_id>_<provider>" and its uuid4 suffix.
_TENANT_CREDENTIAL_BODY = re.compile(r"^cred_(?P<body>\S+)_[0-9a-f]{32}$")


def is_tenant_credential_ref(api_key_ref: str | None) -> bool:
    """Return whether ``api_key_ref`` carries the minted tenant-credential shape.

    A ``True`` answer means exactly one thing to callers: this ref belongs to a
    tenant, so it must never be satisfied by a platform (house) secret. It says
    nothing about whether a value exists -- that stays fail-closed at the store.
    """
    if not api_key_ref:
        return False
    return _TENANT_CREDENTIAL_REF.fullmatch(api_key_ref) is not None


def tenant_hint_from_ref(api_key_ref: str) -> str:
    """Return the tenant identifier carried by a minted ref, for attribution.

    Used for structured logging and error attribution ONLY -- never as a lookup
    key, and never as an authorization input. The parse is best-effort by
    construction: the ref packs ``tenant_id`` and ``provider`` into one
    underscore-joined body and both charsets admit ``_``, so a provider
    containing an underscore shifts the split. Callers must treat the result as
    a hint in a message, not as an identity.

    Returns the literal ref when it does not carry the minted shape, so an error
    message always names something rather than nothing.
    """
    matched = _TENANT_CREDENTIAL_BODY.fullmatch(api_key_ref)
    if matched is None:
        return api_key_ref
    return matched.group("body").rsplit("_", 1)[0]


__all__: list[str] = [
    "TENANT_CREDENTIAL_LANE_REF_PATTERN",
    "is_tenant_credential_ref",
    "tenant_hint_from_ref",
]
