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

import json
import re
import sqlite3
import stat
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from omnimarket.projection.sqlite_database import default_evidence_db_path
from omnimarket.routing.byok_provider_backends import require_byok_plan_permitted
from omnimarket.tenant_credential_ref import is_tenant_credential_ref

#: The local install's tenant identity. A machine running ``onex delegate`` with
#: no gateway has exactly one principal, so the id is a constant rather than a
#: lookup. It carries no secret material and appears in the minted ref.
LOCAL_INSTALL_TENANT_ID = "localinstall"

#: The one table this module adds to the existing local delegation database.
LOCAL_CREDENTIAL_TABLE = "local_inference_credentials"

#: OMN-21059: the ordered models a credential may serve, one row per position.
#: Position 0 is mirrored into the credential row's ``model`` column, so a
#: reader written before the list existed still sees the first model.
LOCAL_CREDENTIAL_MODEL_TABLE = "local_byok_credential_models"

#: OMN-21078: who chose the model stored with a credential.
MODEL_CHOSEN_BY_CUSTOMER: Final[str] = "customer"
MODEL_CHOSEN_BY_CATALOGUE: Final[str] = "catalogue"

_LOCAL_CREDENTIAL_DDL = f"""
CREATE TABLE IF NOT EXISTS {LOCAL_CREDENTIAL_TABLE} (
    secret_ref     TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    secret_value   TEXT NOT NULL,
    registered_at  TEXT NOT NULL DEFAULT (datetime('now')),
    plan           TEXT,
    model          TEXT,
    model_chosen_by TEXT
)
"""

_LOCAL_CREDENTIAL_MODEL_DDL = f"""
CREATE TABLE IF NOT EXISTS {LOCAL_CREDENTIAL_MODEL_TABLE} (
    secret_ref  TEXT NOT NULL REFERENCES {LOCAL_CREDENTIAL_TABLE}(secret_ref)
                ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    model       TEXT NOT NULL,
    chosen_by   TEXT,
    PRIMARY KEY (secret_ref, position)
)
"""


# OMN-18695: the CONTRACT-DECLARED provider credential family, the second key
# space this store holds. A minted ref (above) is what a customer registers by
# PROVIDER on the gap-A path; a declared ref is what a backend's ``secret_ref``
# names in ``bifrost_delegation.yaml`` -- ``llm.openrouter.api_key``,
# ``llm.glm.api_key``, ``llm.gemini.api_key``, ``llm.vertex.access_token``.
# Matched by SHAPE rather than against an enumerated list, so a new backend
# cannot silently fall off the rule.
_DECLARED_PROVIDER_REF: Final[re.Pattern[str]] = re.compile(
    r"^llm\.[A-Za-z0-9_-]+\.[A-Za-z0-9_]+$"
)


def is_local_store_only_ref(api_key_ref: str | None) -> bool:
    """Whether this reference may be answered ONLY by the local store.

    OMN-18695. True for both key spaces this store holds, because both belong
    to a customer rather than to the platform: a minted tenant credential
    (OMN-18694, ``cred_<tenant>_<provider>_<uuid4hex>``) and a
    contract-declared provider credential (``llm.<provider>.<name>``).

    False for everything else, and that is deliberate rather than an
    oversight. A reference such as ``GITHUB_TOKEN`` reaches the resolver from
    a CI runner that has no local store and never will; re-homing it is a
    different question with a different blast radius, and answering it here
    would widen the rule past the credential it is about.
    """
    if not api_key_ref:
        return False
    if is_tenant_credential_ref(api_key_ref):
        return True
    return _DECLARED_PROVIDER_REF.fullmatch(api_key_ref) is not None


def local_secret_remediation(api_key_ref: str) -> str:
    """The command a customer runs to make ``api_key_ref`` resolvable."""
    return f"onex secret set {api_key_ref}"


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
    # OMN-18695: make the file owner-only BEFORE a value can be written into
    # it. ``touch(mode=0o600)`` then ``chmod``, rather than tightening after
    # the write: letting sqlite create it at the umask default leaves a window
    # in which a credential is on disk at 0644. This database is shared with
    # the delegation evidence rows, which the projection path may have created
    # already -- tightening an existing file is the point, not a side effect.
    db_path.touch(mode=0o600, exist_ok=True)
    db_path.chmod(0o600)
    conn = sqlite3.connect(str(db_path))  # no-contract-check: secret-store boundary
    # OMN-17427: close on setup failure so its traceback cannot retain a write lock.
    try:
        conn.row_factory = sqlite3.Row
        # OMN-21059: the model list cascades off its credential row, so a delete or
        # a replace of the row can never leave a stale list behind.
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(_LOCAL_CREDENTIAL_DDL)
        conn.execute(_LOCAL_CREDENTIAL_MODEL_DDL)
        # OMN-20157: a database created before plans existed has no ``plan`` column.
        # ``CREATE TABLE IF NOT EXISTS`` leaves it as it was, so add the column the
        # one time it is missing. Nullable: a row with no plan resolves the
        # provider's default plan, which is what every earlier row meant.
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({LOCAL_CREDENTIAL_TABLE})")
        }
        if "plan" not in columns:
            conn.execute(f"ALTER TABLE {LOCAL_CREDENTIAL_TABLE} ADD COLUMN plan TEXT")
        # OMN-20157: the model the key resolved from the provider's own model list.
        # Nullable on the same terms: a row with no model is resolved at call time.
        if "model" not in columns:
            conn.execute(f"ALTER TABLE {LOCAL_CREDENTIAL_TABLE} ADD COLUMN model TEXT")
        # OMN-21078: only a recorded catalogue pick may be re-aimed. Earlier rows
        # have no record and are treated as the customer's choice.
        if "model_chosen_by" not in columns:
            conn.execute(
                f"ALTER TABLE {LOCAL_CREDENTIAL_TABLE} ADD COLUMN model_chosen_by TEXT"
            )
        # OMN-21059: a credential stored with the single ``model`` column becomes a
        # list of one at position 0, keeping its recorded chooser. Idempotent: a
        # credential that already has list rows is left as it is.
        conn.execute(
            f"INSERT INTO {LOCAL_CREDENTIAL_MODEL_TABLE} "
            "(secret_ref, position, model, chosen_by) "
            f"SELECT secret_ref, 0, model, model_chosen_by FROM {LOCAL_CREDENTIAL_TABLE} "
            "WHERE model IS NOT NULL AND model != '' AND secret_ref NOT IN "
            f"(SELECT secret_ref FROM {LOCAL_CREDENTIAL_MODEL_TABLE})"
        )
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def _normalize_models(
    model: str | None, models: Sequence[str] | None
) -> tuple[str, ...]:
    """The ordered, stripped, duplicate-free models a caller asked to store."""
    if model is not None and models:
        raise LocalByokCredentialError(
            "pass either model or models, not both: models is the ordered list "
            "and model is its one-entry form"
        )
    raw = list(models) if models else ([model] if model else [])
    cleaned = [entry.strip() for entry in raw if entry and entry.strip()]
    if len(set(cleaned)) != len(cleaned):
        raise LocalByokCredentialError(
            "a model appears twice in the list; each model may be chosen once"
        )
    return tuple(cleaned)


def _write_models(
    conn: sqlite3.Connection,
    secret_ref: str,
    models: tuple[str, ...],
    chosen_by: str | None,
) -> None:
    """Replace ``secret_ref``'s ordered models and mirror position 0 on its row."""
    conn.execute(
        f"DELETE FROM {LOCAL_CREDENTIAL_MODEL_TABLE} WHERE secret_ref = ?",
        (secret_ref,),
    )
    conn.executemany(
        f"INSERT INTO {LOCAL_CREDENTIAL_MODEL_TABLE} "
        "(secret_ref, position, model, chosen_by) VALUES (?, ?, ?, ?)",
        [(secret_ref, index, entry, chosen_by) for index, entry in enumerate(models)],
    )
    conn.execute(
        f"UPDATE {LOCAL_CREDENTIAL_TABLE} SET model = ?, model_chosen_by = ? "
        "WHERE secret_ref = ?",
        (
            models[0] if models else None,
            chosen_by if models else None,
            secret_ref,
        ),
    )


def register_local_byok_credential(
    provider: str,
    secret_value: str,
    *,
    plan: str | None = None,
    model: str | None = None,
    models: Sequence[str] | None = None,
    model_chosen_by: str | None = None,
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
        plan: OMN-20157. The provider product the key belongs to (for glm,
            ``general_api``), recorded with the credential so the routing half
            addresses that product's endpoint. ``None`` records nothing and
            resolves the provider's default plan. A plan the catalogue declares
            detection-only (glm ``coding_plan``, barred from third-party systems
            by z.ai's terms) is refused.
        models: OMN-21059. The ordered models the key may serve, first is
            tried first. ``model`` is the one-entry form; pass one or the
            other. Re-registering replaces the whole list.
        model_chosen_by: OMN-21078. Whether the customer or catalogue chose
            the model. ``None`` records no chooser and prevents re-aiming on a
            provider where the customer chooses. No model stores no chooser.
            Every entry of the list carries the same chooser.
        db_path: the local database. Defaults to the existing
            ``~/.omninode/delegation/delegation.sqlite``.

    Returns:
        The minted, tenant-shaped reference. Safe to print and log.

    Raises:
        LocalByokCredentialError: the provider or the value is empty, the
            model chooser is invalid, or the model list repeats a model.
        ByokPlanNotPermittedError: ``plan`` is a detection-only plan. Nothing is
            written.
    """
    if model_chosen_by not in (
        None,
        MODEL_CHOSEN_BY_CUSTOMER,
        MODEL_CHOSEN_BY_CATALOGUE,
    ):
        raise LocalByokCredentialError(
            "model_chosen_by must be 'customer', 'catalogue', or None"
        )
    normalized = provider.strip().lower()
    if not normalized:
        raise LocalByokCredentialError("provider must be a non-empty string")
    if plan:
        require_byok_plan_permitted(normalized, plan)
    if not secret_value or not secret_value.strip():
        raise LocalByokCredentialError(
            f"no value supplied for provider {normalized!r}; a blank credential "
            "would register a reference that can never resolve"
        )
    chosen_models = _normalize_models(model, models)
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
            "(secret_ref, provider, secret_value, plan) VALUES (?, ?, ?, ?)",
            (
                ref,
                normalized,
                secret_value.strip(),
                plan.strip().lower() if plan else None,
            ),
        )
        _write_models(conn, ref, chosen_models, model_chosen_by)
        conn.commit()
    finally:
        conn.close()
    return ref


def set_local_byok_credential_models(
    provider: str,
    models: Sequence[str],
    *,
    model_chosen_by: str | None = None,
    db_path: Path | None = None,
) -> bool:
    """Replace the ordered models of ``provider``'s stored credential, keeping its key.

    OMN-21059. The whole list is written at once, so an add, a reorder and a
    remove are each one replacement and a reader never sees a half-edited list.
    Returns whether a credential for ``provider`` exists; ``False`` writes
    nothing. An empty ``models`` clears the list.

    Raises:
        LocalByokCredentialError: the chooser is invalid or a model repeats.
    """
    if model_chosen_by not in (
        None,
        MODEL_CHOSEN_BY_CUSTOMER,
        MODEL_CHOSEN_BY_CATALOGUE,
    ):
        raise LocalByokCredentialError(
            "model_chosen_by must be 'customer', 'catalogue', or None"
        )
    chosen_models = _normalize_models(None, models)
    ref = resolve_local_byok_credential_ref(provider, db_path=db_path)
    if ref is None:
        return False
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    conn = _connect(resolved_path)
    try:
        _write_models(conn, ref, chosen_models, model_chosen_by)
        conn.commit()
    finally:
        conn.close()
    return True


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


def resolve_local_byok_credential_plan(
    provider: str, *, db_path: Path | None = None
) -> str | None:
    """Return the plan recorded with the registered credential for ``provider``.

    OMN-20157. ``None`` when no credential is registered or none recorded a plan;
    both mean "resolve the provider's default plan". Never returns a value.
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
            f"SELECT plan FROM {LOCAL_CREDENTIAL_TABLE} WHERE provider = ? "
            "ORDER BY registered_at DESC LIMIT 1",
            (normalized,),
        ).fetchone()
    finally:
        conn.close()
    return str(row["plan"]) if row is not None and row["plan"] else None


def resolve_local_byok_credential_models(
    provider: str, *, db_path: Path | None = None
) -> tuple[str, ...]:
    """Return the ordered models stored with the credential for ``provider``.

    OMN-21059. Empty when no credential is registered or none holds a model;
    the route then carries the unresolved marker and the effect resolves the
    model from the provider's list before the call. A credential written
    straight into the ``model`` column, with no list rows, reads as a list of
    that one model. Never returns a value.
    """
    normalized = provider.strip().lower()
    if not normalized:
        return ()
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    if not resolved_path.is_file():
        return ()
    conn = _connect(resolved_path)
    try:
        row = conn.execute(
            f"SELECT secret_ref, model FROM {LOCAL_CREDENTIAL_TABLE} "
            "WHERE provider = ? ORDER BY registered_at DESC LIMIT 1",
            (normalized,),
        ).fetchone()
        if row is None:
            return ()
        listed = conn.execute(
            f"SELECT model FROM {LOCAL_CREDENTIAL_MODEL_TABLE} "
            "WHERE secret_ref = ? ORDER BY position",
            (row["secret_ref"],),
        ).fetchall()
    finally:
        conn.close()
    if listed:
        return tuple(str(entry["model"]) for entry in listed)
    return (str(row["model"]),) if row["model"] else ()


def resolve_local_byok_credential_model(
    provider: str, *, db_path: Path | None = None
) -> str | None:
    """Return the first model stored with the credential for ``provider``.

    OMN-20157. ``None`` when no credential is registered or none resolved a
    model; the route then carries the unresolved marker and the effect resolves
    the model from the provider's list before the call. OMN-21059: the first
    entry of :func:`resolve_local_byok_credential_models`. Never returns a value.
    """
    models = resolve_local_byok_credential_models(provider, db_path=db_path)
    return models[0] if models else None


def resolve_local_byok_model_chooser(
    secret_ref: str | None, *, db_path: Path | None = None
) -> str | None:
    """Return who chose the model stored with credential ``secret_ref``.

    OMN-21078. ``None`` for an absent database, an unknown reference or no
    recorded chooser; on a provider where the customer chooses, all three
    are treated as the customer's choice.
    """
    if not secret_ref:
        return None
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    if not resolved_path.is_file():
        return None
    conn = _connect(resolved_path)
    try:
        row = conn.execute(
            f"SELECT model_chosen_by FROM {LOCAL_CREDENTIAL_TABLE} WHERE secret_ref = ?",
            (secret_ref,),
        ).fetchone()
    finally:
        conn.close()
    return (
        str(row["model_chosen_by"])
        if row is not None and row["model_chosen_by"]
        else None
    )


def record_local_byok_model(
    secret_ref: str, model: str, *, db_path: Path | None = None
) -> bool:
    """Store ``model`` as a catalogue pick for credential ``secret_ref``.

    OMN-20157. Called by the effect after a call-time re-resolve, so the next
    delegation runs the model the provider now offers instead of re-learning it.
    OMN-21078: records that the catalogue chose it, so a later call may re-aim it.
    Returns whether a row was updated; an absent database or an unknown
    reference updates nothing and is not an error.
    """
    if not secret_ref or not model.strip():
        return False
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    if not resolved_path.is_file():
        return False
    conn = _connect(resolved_path)
    try:
        known = conn.execute(
            f"SELECT 1 FROM {LOCAL_CREDENTIAL_TABLE} WHERE secret_ref = ?",
            (secret_ref,),
        ).fetchone()
        if known is None:
            return False
        # OMN-21059: a catalogue pick is a list of one; it replaces the list.
        _write_models(conn, secret_ref, (model.strip(),), MODEL_CHOSEN_BY_CATALOGUE)
        conn.commit()
        return True
    finally:
        conn.close()


def registered_local_byok_providers(
    *, db_path: Path | None = None, registration_order: bool = False
) -> tuple[str, ...]:
    """Return registered providers, sorted or in registration order.

    Registration timestamps have second resolution; rowid breaks ties in the
    order of insertion. This is a fallback, not a customer ordering preference.
    """
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    if not resolved_path.is_file():
        return ()
    conn = _connect(resolved_path)
    try:
        rows = conn.execute(
            f"SELECT provider FROM {LOCAL_CREDENTIAL_TABLE} ORDER BY registered_at, rowid"
        ).fetchall()
    finally:
        conn.close()
    providers = tuple(dict.fromkeys(str(row["provider"]) for row in rows))
    return providers if registration_order else tuple(sorted(providers))


#: The last pinned test of each provider, written by ``onex models add`` and
#: ``onex models test`` beside the local credential database (OMN-20817).
LOCAL_MODEL_TESTS_FILE: Final = "model_tests.json"


def local_model_tests_path(db_path: Path | None = None) -> Path:
    """Where the last pinned test of each provider is recorded."""
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    return resolved_path.parent / LOCAL_MODEL_TESTS_FILE


def local_byok_key_test_failed(provider: str, *, db_path: Path | None = None) -> bool:
    """True when ``provider``'s key failed its last pinned test (OMN-21259).

    Delegation does not route such a key until a test passes: its only chosen
    model the test proves did not answer, so every run would climb to it and
    fail. A key never tested, or one whose record is unreadable, stays routable.
    """
    try:
        recorded = json.loads(
            local_model_tests_path(db_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    entry = recorded.get(provider) if isinstance(recorded, dict) else None
    return isinstance(entry, dict) and entry.get("status") == "failed"


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
        # OMN-18695: read the mode BEFORE connecting. ``_connect`` tightens the
        # file to 0600, which is right for a write and would erase the evidence
        # here -- a file that was world-readable when this read began is the
        # fact worth refusing on, and re-tightening it first would make every
        # read look clean.
        mode_at_open = stat.S_IMODE(self._db_path.stat().st_mode)
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
        if not value:
            return None
        # OMN-18695: refuse to hand back a credential out of a file others can
        # read. Checked on READ as well as on write because a file survives
        # ``chmod``, backup/restore and ``scp``, so a write-time check proves
        # nothing about the file actually loaded (the reasoning
        # ``StoreOnexHomeFiles`` established for ``~/.onex/credentials.json``).
        #
        # Conditional on a value EXISTING, deliberately: a machine whose
        # evidence database the projection created at the umask default and
        # which has registered no credential at all must get the actionable
        # "not registered, run this command" refusal, not a permission
        # complaint about a file holding no secret.
        if mode_at_open & 0o077:
            raise PermissionError(
                f"{self._db_path} is mode {mode_at_open:04o}; it must be 0600 "
                "(owner-only). Refusing to read a credential out of a group- "
                f"or world-readable file. Fix with: chmod 600 {self._db_path}"
            )
        return value

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


def local_credential_registered_at(
    secret_ref: str, *, db_path: Path | None = None
) -> str | None:
    """When this reference was last written. OMN-18695, for ``onex secret list``.

    A timestamp, never a value. ``None`` for an absent database or an
    unregistered reference, on the same nullable terms as the resolver above.
    """
    resolved_path = db_path if db_path is not None else default_evidence_db_path()
    if not resolved_path.is_file():
        return None
    conn = _connect(resolved_path)
    try:
        row = conn.execute(
            f"SELECT registered_at FROM {LOCAL_CREDENTIAL_TABLE} WHERE secret_ref = ?",
            (secret_ref,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return str(row["registered_at"] or "") or None


__all__: list[str] = [
    "LOCAL_CREDENTIAL_MODEL_TABLE",
    "LOCAL_CREDENTIAL_TABLE",
    "LOCAL_INSTALL_TENANT_ID",
    "LOCAL_MODEL_TESTS_FILE",
    "MODEL_CHOSEN_BY_CATALOGUE",
    "MODEL_CHOSEN_BY_CUSTOMER",
    "LocalByokCredentialError",
    "LocalByokCredentialStore",
    "is_local_store_only_ref",
    "local_byok_key_test_failed",
    "local_credential_registered_at",
    "local_model_tests_path",
    "local_secret_remediation",
    "mint_local_byok_credential_ref",
    "register_local_byok_credential",
    "registered_local_byok_providers",
    "resolve_local_byok_credential_model",
    "resolve_local_byok_credential_models",
    "resolve_local_byok_credential_plan",
    "resolve_local_byok_credential_ref",
    "resolve_local_byok_model_chooser",
    "revoke_local_byok_credential",
    "set_local_byok_credential_models",
]
