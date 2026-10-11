# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Store a secret found in captured content, and hand back its reference (OMN-20926).

Operator ruling 2026-10-10 (RULING row 2026-10-10T22:38:48Z,
lane=secret-refs-9f8a): when the content scrubber finds a secret in captured
content, the secret is stored in the secret store and the record carries a
reference in its place. Anything that needs the value resolves the reference
through the store, which checks permission and logs access. Ciphertext never
goes on the bus.

Where a secret lands
--------------------
One key per distinct value: the capture-redaction contract's
``store_key_template`` filled with the value's digest, in the folder the
machine's private overlay names. The digest is HMAC-SHA256 of the value under a
per-deployment reference key, so the same value always lands on the same key
(deduplicated) and the bus never carries an unsalted hash of a low-entropy
secret. The secret's comment carries the capturing session and the time.

The write is CREATE ONLY. An existing key is the dedupe case and is accepted
without reading or updating it, so the writer identity needs neither a read nor
an edit right on the namespace: a compromised writer cannot read back what it
stored.

Configuration
-------------
``<onex_home>/config.yaml`` block ``captured_secret_store`` (addresses, the
writer's client id and two references) with the values in
``<onex_home>/credentials.json``, refused unless mode 0600: the split
``onex auth login`` already uses. Nothing deployment specific is committed.

Failure
-------
Every failure is a typed outcome with no reference, and the first one is
sticky for the handler's life: one unreachable store costs one timeout per
capture, not one per secret. Nothing here logs or returns a value, a digest or
a credential.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import SecretStr

from omnimarket.models.captured_secret.model_captured_secret_store_overlay import (
    CAPTURED_SECRET_STORE_BLOCK,
    load_captured_secret_store_overlay,
)
from omnimarket.nodes.node_captured_secret_store_effect.models.model_captured_secret_store_request import (
    ModelCapturedSecretStoreRequest,
)
from omnimarket.nodes.node_captured_secret_store_effect.models.model_captured_secret_store_result import (
    EnumCapturedSecretStoreOutcome,
    ModelCapturedSecretStoreResult,
)
from omnimarket.nodes.node_event_emit_effect.redaction import (
    SecretReference,
    load_contract,
)

logger = logging.getLogger(__name__)

#: One API call may take this long. A store that does not answer must not hold
#: a backgrounded capture process open.
TIMEOUT_SECONDS = 5.0

#: The two schemes a store address may use; anything else is refused unopened.
_CONNECTIONS: dict[str, type[http.client.HTTPConnection]] = {
    "https": http.client.HTTPSConnection,
    "http": http.client.HTTPConnection,
}


class CapturedSecretWriterConfigError(ValueError):
    """The writer configuration is present and unusable. Names keys, never values."""


@dataclass(frozen=True)
class CapturedSecretWriterConfig:
    """The writer's resolved configuration. Secrets are excluded from repr."""

    infisical_addr: str
    project_id: str
    environment_slug: str
    secret_path: str
    client_id: str
    client_secret: SecretStr = field(repr=False)
    reference_key: SecretStr = field(repr=False)


def load_writer_config(onex_home: Path) -> CapturedSecretWriterConfig | None:
    """The writer configuration, or ``None`` when this machine has none.

    ``None`` means no ``captured_secret_store`` block, or a block naming no
    writer identity: this machine does not take part.

    Raises:
        CapturedSecretWriterConfigError: the block is present but unusable.
    """
    from omnibase_core.errors.model_onex_error import ModelOnexError
    from omnibase_infra.cli.store_onex_home_files import StoreOnexHomeFiles

    files = StoreOnexHomeFiles(onex_home)
    try:
        document = files.load_config(must_exist=False)
    except ModelOnexError as exc:
        raise CapturedSecretWriterConfigError(exc.message) from None
    if CAPTURED_SECRET_STORE_BLOCK not in document:
        return None
    try:
        overlay = load_captured_secret_store_overlay(
            document, source=str(files.config_path)
        )
    except ValueError as exc:
        raise CapturedSecretWriterConfigError(str(exc)) from None
    if overlay.writer_client_id is None:
        return None
    named = {
        "writer_client_secret_ref": overlay.writer_client_secret_ref,
        "reference_key_ref": overlay.reference_key_ref,
    }
    refs = {name: ref for name, ref in named.items() if ref is not None}
    missing = sorted(set(named) - set(refs))
    if missing:
        raise CapturedSecretWriterConfigError(
            f"{files.config_path} block '{CAPTURED_SECRET_STORE_BLOCK}' names a "
            f"writer but not {missing}"
        )
    values: dict[str, SecretStr] = {}
    for name, ref in refs.items():
        remediation = f"file it in credentials.json under '{ref}' (mode 0600)"
        try:
            values[name] = SecretStr(files.read_secret(ref, remediation))
        except ModelOnexError as exc:
            raise CapturedSecretWriterConfigError(exc.message) from None
    return CapturedSecretWriterConfig(
        infisical_addr=overlay.infisical_addr.rstrip("/"),
        project_id=str(overlay.project_id),
        environment_slug=overlay.environment_slug,
        secret_path=overlay.secret_path,
        client_id=overlay.writer_client_id,
        client_secret=values["writer_client_secret_ref"],
        reference_key=values["reference_key_ref"],
    )


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------


class StoreUnreachableError(RuntimeError):
    """The store did not answer. Carries no request content."""


class ProtocolCreateOnlySecretStore(Protocol):
    """The two calls the writer makes. A fake store implements this in tests."""

    def login(self, config: CapturedSecretWriterConfig) -> str | None:
        """An access token, or ``None`` when the store refused the identity.

        Raises:
            StoreUnreachableError: the store did not answer.
        """
        ...

    def create(
        self,
        config: CapturedSecretWriterConfig,
        token: str,
        *,
        key: str,
        value: SecretStr,
        comment: str,
    ) -> str:
        """``created``, ``exists``, ``refused`` or ``failed_<status>``.

        Raises:
            StoreUnreachableError: the store did not answer.
        """
        ...


class InfisicalCreateOnlySecretStore:
    """Infisical's REST API: universal-auth login and a raw secret create.

    The same two calls the lab store publisher makes, with the same posture: an
    error body is parsed for its message only and never surfaced (a store
    error can echo the request, and the request carries the value). Standard
    library only, because the producer that calls it is a hook process.
    """

    def __init__(self, *, timeout: float = TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def _post(
        self, url: str, payload: dict[str, object], token: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        parts = urllib.parse.urlsplit(url)
        connection_class = _CONNECTIONS.get(parts.scheme)
        if connection_class is None or not parts.hostname:
            raise StoreUnreachableError("unsupported store address")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        connection = connection_class(parts.hostname, parts.port, timeout=self._timeout)
        try:
            connection.request(
                "POST", path, body=json.dumps(payload).encode("utf-8"), headers=headers
            )
            response = connection.getresponse()
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
        except (OSError, http.client.HTTPException) as error:
            raise StoreUnreachableError(type(error).__name__) from None
        finally:
            connection.close()
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}

    def login(self, config: CapturedSecretWriterConfig) -> str | None:
        status, body = self._post(
            f"{config.infisical_addr}/api/v1/auth/universal-auth/login",
            {
                "clientId": config.client_id,
                "clientSecret": config.client_secret.get_secret_value(),
            },
        )
        token = body.get("accessToken") if status == 200 else None
        return token if isinstance(token, str) and token else None

    def create(
        self,
        config: CapturedSecretWriterConfig,
        token: str,
        *,
        key: str,
        value: SecretStr,
        comment: str,
    ) -> str:
        status, body = self._post(
            f"{config.infisical_addr}/api/v3/secrets/raw/{urllib.parse.quote(key)}",
            {
                "workspaceId": config.project_id,
                "environment": config.environment_slug,
                "secretPath": config.secret_path,
                "secretValue": value.get_secret_value(),
                "secretComment": comment,
                "type": "shared",
            },
            token=token,
        )
        if status in (200, 201):
            return "created"
        message = str(body.get("message", "")).lower()
        # The dedupe case: 409 on current servers, 400 "already exist" on older
        # ones. Any other 400 is a failure, not an existing secret: accepting it
        # would mint a reference to nothing.
        if status == 409 or (status == 400 and "already exist" in message):
            return "exists"
        if status in (401, 403):
            return "refused"
        return f"failed_{status}"


# ---------------------------------------------------------------------------
# the handler
# ---------------------------------------------------------------------------


class HandlerCapturedSecretStore:
    """``handle(request) -> result``: one secret in, one reference or a reason out.

    Loads its configuration on the first request, so a producer can build one
    per capture and pay nothing when no secret is found. Logs in once, caches
    the references it minted, and after the first failure refuses for the rest
    of its life.
    """

    def __init__(
        self,
        *,
        onex_home: Path,
        store: ProtocolCreateOnlySecretStore | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._onex_home = onex_home
        self._store = store if store is not None else InfisicalCreateOnlySecretStore()
        self._contract_path = contract_path
        self._loaded = False
        self._config: CapturedSecretWriterConfig | None = None
        self._reference: SecretReference | None = None
        self._token: str | None = None
        self._minted: dict[str, str] = {}
        self._refusal: ModelCapturedSecretStoreResult | None = None

    def _refuse(
        self, outcome: EnumCapturedSecretStoreOutcome, detail: str | None = None
    ) -> ModelCapturedSecretStoreResult:
        result = ModelCapturedSecretStoreResult(outcome=outcome, detail=detail)
        self._refusal = result
        logger.warning("captured secret not stored: %s (%s)", outcome.value, detail)
        return result

    def _configure(self) -> ModelCapturedSecretStoreResult | None:
        """Load config and contract once; a refusal if this machine cannot write."""
        if self._loaded:
            return None
        self._loaded = True
        self._reference = load_contract(self._contract_path).secret_reference
        if self._reference is None:
            return self._refuse(
                EnumCapturedSecretStoreOutcome.MISCONFIGURED,
                "the capture-redaction contract declares no secret_reference",
            )
        try:
            self._config = load_writer_config(self._onex_home)
        except CapturedSecretWriterConfigError as exc:
            return self._refuse(EnumCapturedSecretStoreOutcome.MISCONFIGURED, str(exc))
        if self._config is None:
            return self._refuse(EnumCapturedSecretStoreOutcome.NOT_CONFIGURED)
        return None

    def handle(
        self, request: ModelCapturedSecretStoreRequest
    ) -> ModelCapturedSecretStoreResult:
        if self._refusal is not None:
            return self._refusal
        refused = self._configure()
        if refused is not None:
            return refused
        config, reference = self._config, self._reference
        if config is None or reference is None:  # unreachable after _configure
            return self._refuse(EnumCapturedSecretStoreOutcome.MISCONFIGURED)
        digest = hmac.new(
            config.reference_key.get_secret_value().encode("utf-8"),
            request.value.get_secret_value().encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        minted = self._minted.get(digest)
        if minted is not None:
            return ModelCapturedSecretStoreResult(
                outcome=EnumCapturedSecretStoreOutcome.EXISTS, reference=minted
            )
        try:
            if self._token is None:
                self._token = self._store.login(config)
                if self._token is None:
                    return self._refuse(EnumCapturedSecretStoreOutcome.LOGIN_REFUSED)
            created = self._store.create(
                config,
                self._token,
                key=reference.store_key_for(digest),
                value=request.value,
                comment=(
                    f"captured session={request.session_id} "
                    f"at={request.captured_at.isoformat(timespec='seconds')}"
                ),
            )
        except StoreUnreachableError as exc:
            return self._refuse(
                EnumCapturedSecretStoreOutcome.STORE_UNREACHABLE, str(exc)
            )
        if created == "refused":
            return self._refuse(EnumCapturedSecretStoreOutcome.WRITE_REFUSED)
        if created not in ("created", "exists"):
            return self._refuse(EnumCapturedSecretStoreOutcome.WRITE_FAILED, created)
        minted = reference.reference_for(digest)
        self._minted[digest] = minted
        return ModelCapturedSecretStoreResult(
            outcome=(
                EnumCapturedSecretStoreOutcome.STORED
                if created == "created"
                else EnumCapturedSecretStoreOutcome.EXISTS
            ),
            reference=minted,
        )


__all__ = [
    "CapturedSecretWriterConfig",
    "CapturedSecretWriterConfigError",
    "HandlerCapturedSecretStore",
    "InfisicalCreateOnlySecretStore",
    "ProtocolCreateOnlySecretStore",
    "StoreUnreachableError",
    "load_writer_config",
]
