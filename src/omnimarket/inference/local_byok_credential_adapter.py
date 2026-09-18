# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer's own provider key, held in the local SQLite store (OMN-18694/OMN-18695).

On the hosted path a customer's BYOK credential is minted by
``POST /v1/tenants/me/inference-credentials``, written into Infisical, and
projected into ``delegation_routing_tenant_overlay``. A customer machine has
none of that: no gateway, no Infisical, no Postgres, no broker. This module is
the local half of the same credential plane — the *store* the effect boundary
resolves a minted reference through when nothing hosted is reachable.

What it is NOT
--------------
It is not a new store technology. The operator's ruling of 2026-09-18 is that
the store on a customer machine is **the SQLite store that already exists**:
``~/.omninode/delegation/delegation.sqlite``, the same file
``omnimarket.projection.sqlite_database`` already materialises local delegation
evidence into. This module adds one table to that file and a
``ProtocolSecretStore`` view over it. There is no keychain handler and no
second database.

The invariant this exists to hold
---------------------------------
The reference is public and the value is not. ``register_local_byok_credential``
returns the minted ref — safe to print, log and display — and there is no
function anywhere in this module that returns a stored value to a caller as a
plain string other than :meth:`LocalByokCredentialStore.get_secret`, which is
the ``ProtocolSecretStore`` read the effect boundary funnels through and which
wraps its result in ``SecretStr`` one frame later. Nothing writes a value to a
log, and nothing accepts one on a command line: ``register_local_byok_credential``
takes the value as an argument from a caller that read it from stdin, never
from ``sys.argv`` (a value on argv is visible in ``ps`` to every process on the
host — memory ``reference_secrets_on_argv_are_visible_in_ps``).

Ref shape, and why it is the minted one
---------------------------------------
:func:`mint_local_byok_credential_ref` produces exactly the shape
``omnimarket.projection.credential_publisher.mint_api_key_ref`` produces --
``cred_{tenant}_{provider}_{uuid4hex}`` -- because that shape is already the
single authority for "this is a TENANT's key, it must never be satisfied by a
house secret" (``omnimarket.tenant_credential_ref``, OMN-16944). Reusing it
means the local path inherits that refusal at the choke point every effect-
boundary call site already funnels through, rather than re-deriving it.

The local install's tenant id is the constant :data:`LOCAL_INSTALL_TENANT_ID`.
A local install has exactly one identity by construction -- there is no
authenticated principal to read one from -- and naming it explicitly is what
keeps the minted ref recognisable as tenant-shaped rather than house-shaped.

Related:
    - OMN-18694: gap A, a customer's OpenRouter key routes a local delegation
    - OMN-18695: gap B, the existing SQLite store resolves a key reference
    - OMN-16944: tenant-shaped refs resolve tenant-scoped, never env-fallback
    - OMN-17372 / OMN-17082: no house credential may answer customer work
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from omnimarket.projection.sqlite_database import default_evidence_db_path

#: The local install's tenant identity. A machine running ``onex delegate`` with
#: no gateway has exactly one principal, so the id is a constant rather than a
#: lookup. It carries no secret material and appears in the minted ref.
LOCAL_INSTALL_TENANT_ID = "localinstall"

#: The one table this module adds to the existing local delegation database.
LOCAL_CREDENTIAL_TABLE = "local_inference_credentials"

_LOCAL_CREDENTIAL_DDL = f"""
CREATE TABLE IF NOT EXISTS {LOCAL_CREDENTIAL_TABLE} (
    secret_ref     TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    secret_value   TEXT NOT NULL,
    registered_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


class LocalByokCredentialError(RuntimeError):
    """A local BYOK credential could not be registered or read.

    Deliberately distinct from ``SecretResolutionError``: a store that cannot
    be opened and a reference that has no value are different facts, and the
    second one is the customer's to fix while the first one is not.
    """


def mint_local_byok_credential_ref(provider: str) -> str:
    """Mint a tenant-shaped reference for a locally registered provider key.

    Same shape as the hosted minter (``credential_publisher.mint_api_key_ref``)
    so ``is_tenant_credential_ref`` recognises it and the effect boundary
    refuses it the house-key fallback path. Carries no secret material.
    """
    normalized = provider.strip().lower()
    if not normalized:
        raise LocalByokCredentialError("provider must be a non-empty string")
    return f"cred_{LOCAL_INSTALL_TENANT_ID}_{normalized}_{uuid.uuid4().hex}"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the existing local delegation database and ensure the one table.

    This IS the ``ProtocolSecretStore`` I/O boundary adapter for the local
    install -- the connection is the adapter's purpose, not a freestanding
    imperative call. The file name ends in ``_adapter.py``, which is the
    sanctioned boundary for a direct ``sqlite3.connect`` (omnibase_core
    ``node_no_raw_sqlite3_check_compute``).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))  # no-contract-check: secret-store boundary
    conn.row_factory = sqlite3.Row
    conn.execute(_LOCAL_CREDENTIAL_DDL)
    conn.commit()
    return conn


def register_local_byok_credential(
    provider: str,
    secret_value: str,
    *,
    db_path: Path | None = None,
) -> str:
    """Store ``secret_value`` under a freshly minted ref and return the ref.

    The returned reference is what a contract, an overlay, a log line and a
    receipt may carry. The value is written only into the local database.

    Any previously registered credential for the same provider is REPLACED:
    a local install has one identity and one key per provider, and leaving a
    superseded row behind would make "which key answered" unresolvable.

    Args:
        provider: the provider id the customer is bringing a key for, matched
            against the declared BYOK catalogue by the routing half.
        secret_value: the key itself, read by the caller from stdin. Never
            read from ``sys.argv`` by this function or any caller of it.
        db_path: the local database. Defaults to the existing
            ``~/.omninode/delegation/delegation.sqlite``.

    Returns:
        The minted, tenant-shaped reference. Safe to print and log.

    Raises:
        LocalByokCredentialError: the provider or the value is empty.
    """
    normalized = provider.strip().lower()
    if not normalized:
        raise LocalByokCredentialError("provider must be a non-empty string")
    if not secret_value or not secret_value.strip():
        raise LocalByokCredentialError(
            f"no value supplied for provider {normalized!r}; a blank credential "
            "would register a reference that can never resolve"
        )
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    ref = mint_local_byok_credential_ref(normalized)
    conn = _connect(resolved_path)
    try:
        conn.execute(
            f"DELETE FROM {LOCAL_CREDENTIAL_TABLE} WHERE provider = ?",
            (normalized,),
        )
        conn.execute(
            f"INSERT INTO {LOCAL_CREDENTIAL_TABLE} "
            "(secret_ref, provider, secret_value) VALUES (?, ?, ?)",
            (ref, normalized, secret_value.strip()),
        )
        conn.commit()
    finally:
        conn.close()
    return ref


def revoke_local_byok_credential(provider: str, *, db_path: Path | None = None) -> int:
    """Delete every locally registered credential for ``provider``.

    Returns the number of rows removed, so a caller can tell "revoked" from
    "there was nothing there".
    """
    normalized = provider.strip().lower()
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    if not resolved_path.is_file():
        return 0
    conn = _connect(resolved_path)
    try:
        cursor = conn.execute(
            f"DELETE FROM {LOCAL_CREDENTIAL_TABLE} WHERE provider = ?",
            (normalized,),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def resolve_local_byok_credential_ref(
    provider: str, *, db_path: Path | None = None
) -> str | None:
    """Return the registered reference for ``provider``, or ``None``.

    ``None`` is the fail-CLOSED answer for every ambiguous case: an absent
    database file, a database with no credential table, and a provider with no
    row all report "no local BYOK credential" rather than raising, because the
    routing half's correct response to all three is identical -- mint no BYOK
    route. A caller that needs the VALUE still fails closed at the store.

    Never returns a value, only a reference.
    """
    normalized = provider.strip().lower()
    if not normalized:
        return None
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    if not resolved_path.is_file():
        return None
    conn = _connect(resolved_path)
    try:
        row = conn.execute(
            f"SELECT secret_ref FROM {LOCAL_CREDENTIAL_TABLE} WHERE provider = ? "
            "ORDER BY registered_at DESC LIMIT 1",
            (normalized,),
        ).fetchone()
    finally:
        conn.close()
    return str(row["secret_ref"]) if row is not None else None


def registered_local_byok_providers(*, db_path: Path | None = None) -> tuple[str, ...]:
    """Return every provider with a locally registered credential, sorted."""
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    if not resolved_path.is_file():
        return ()
    conn = _connect(resolved_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT provider FROM {LOCAL_CREDENTIAL_TABLE}"
        ).fetchall()
    finally:
        conn.close()
    return tuple(sorted(str(row["provider"]) for row in rows))


class LocalByokCredentialStore:
    """``ProtocolSecretStore`` over the existing local delegation database.

    Read-mostly by design: ``set_secret`` and ``delete_secret`` are supported
    because a local install has no other writer (there is no intake API to mint
    through), but a caller registering a credential should use
    :func:`register_local_byok_credential`, which mints the reference rather
    than accepting a caller-supplied one.

    ``get_secret`` returns ``None`` for a missing reference and never raises for
    an absent database -- the nullable-lookup contract ``ProtocolSecretStore``
    declares (OMN-10556). Fail-closed on a MISSING VALUE is the caller's
    posture, applied one frame up in ``resolve_api_key_async``.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path if db_path is not None else default_evidence_db_path()

    @property
    def db_path(self) -> Path:
        """The local database this store reads. Carries no secret material."""
        return self._db_path

    async def get_secret(self, key: str) -> str | None:
        if not key or not self._db_path.is_file():
            return None
        conn = _connect(self._db_path)
        try:
            row = conn.execute(
                f"SELECT secret_value FROM {LOCAL_CREDENTIAL_TABLE} "
                "WHERE secret_ref = ?",
                (key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        value = str(row["secret_value"])
        return value or None

    async def set_secret(self, key: str, value: str) -> bool:
        if not key or not value:
            return False
        conn = _connect(self._db_path)
        try:
            conn.execute(
                f"INSERT INTO {LOCAL_CREDENTIAL_TABLE} "
                "(secret_ref, provider, secret_value) VALUES (?, ?, ?) "
                "ON CONFLICT(secret_ref) DO UPDATE SET secret_value = excluded.secret_value",
                (key, _provider_from_ref(key), value),
            )
            conn.commit()
        finally:
            conn.close()
        return True

    async def delete_secret(self, key: str) -> bool:
        if not self._db_path.is_file():
            return False
        conn = _connect(self._db_path)
        try:
            cursor = conn.execute(
                f"DELETE FROM {LOCAL_CREDENTIAL_TABLE} WHERE secret_ref = ?",
                (key,),
            )
            conn.commit()
            return bool(cursor.rowcount)
        finally:
            conn.close()

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        """Return registered REFERENCES (never values), optionally prefix-filtered."""
        if not self._db_path.is_file():
            return []
        conn = _connect(self._db_path)
        try:
            rows = conn.execute(
                f"SELECT secret_ref FROM {LOCAL_CREDENTIAL_TABLE}"
            ).fetchall()
        finally:
            conn.close()
        refs = [str(row["secret_ref"]) for row in rows]
        if prefix:
            refs = [ref for ref in refs if ref.startswith(prefix)]
        return sorted(refs)

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
        return


def _provider_from_ref(ref: str) -> str:
    """Best-effort provider slug out of a minted ref, for the row's own column.

    Attribution only -- the routing half resolves a provider to a ref, never a
    ref back to a provider, so a mis-split here cannot misroute anything.
    """
    body = ref[len("cred_") :] if ref.startswith("cred_") else ref
    parts = body.rsplit("_", 2)
    return parts[-2] if len(parts) >= 2 else "unknown"


__all__: list[str] = [
    "LOCAL_CREDENTIAL_TABLE",
    "LOCAL_INSTALL_TENANT_ID",
    "LocalByokCredentialError",
    "LocalByokCredentialStore",
    "mint_local_byok_credential_ref",
    "register_local_byok_credential",
    "registered_local_byok_providers",
    "resolve_local_byok_credential_ref",
    "revoke_local_byok_credential",
]
