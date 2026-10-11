# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret store EFFECT's handler: resolve, create-only put, list (OMN-20944).

``handle(request: ModelSecretStoreRequest) -> ModelSecretStoreResult``. The
handler is vendor-neutral: it depends only on the omnibase_spi
``ProtocolSecretStore`` seam, and the store behind it comes from the node's
``adapters`` package by the provider the machine's private overlay names.

Naming a secret
---------------
Either a folder and a key, or a ``secret://onex/<folder segments>/<key>``
reference. A reference whose first segment is a namespace the contract's
``reference_namespaces`` declares maps through that namespace's folder and key
template (``secret://onex/captured/<digest>`` is key ``CAPTURED_<digest>`` in
folder ``/captured``). Every folder is relative to the overlay's root folder.

Identity
--------
The overlay names the machine identity's client id and secret by reference.
They resolve at run time from the injected bootstrap ``ProtocolSecretStore``
(the runtime's own store) or, with none injected, from
``<onex_home>/credentials.json``, refused unless mode 0600. No environment
variable is required and nothing deployment specific is committed.

Never a value
-------------
The value of a create and of a resolve is a ``SecretStr`` excluded from
serialization, so no event carries one. Logs and ``detail`` name operations,
folders, keys, outcomes and exception types, never a value or a credential.
Every failure is a typed outcome; nothing raises out of ``handle``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml
from omnibase_spi.protocols.services import ProtocolSecretStore
from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.adapters import secret_store_for
from omnimarket.nodes.node_secret_store_effect.models.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
    load_secret_store_overlay,
)
from omnimarket.nodes.node_secret_store_effect.models.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.nodes.node_secret_store_effect.models.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)

if TYPE_CHECKING:
    from omnibase_infra.cli.store_onex_home_files import StoreOnexHomeFiles

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_REFERENCE_PREFIX = "secret://onex/"
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
#: The shapes of message the node's own adapters raise; anything else is
#: reported by exception type alone, since a foreign store may echo a request.
_SAFE_MESSAGE = re.compile(r"^[a-z]+ (refused|unavailable|rejected|failed)[\w :]*$")


@dataclass(frozen=True)
class _Namespace:
    folder: str
    key_template: str


@dataclass(frozen=True)
class _Target:
    folder: str
    key: str | None


@dataclass(frozen=True)
class _Resolved:
    store: ProtocolSecretStore | None = None
    root: str = "/"
    refusal: EnumSecretStoreOutcome | None = None
    detail: str | None = None


class _InvalidRequestError(ValueError):
    """A request whose fields do not name exactly one secret or folder."""


def _load_namespaces(contract_path: Path) -> dict[str, _Namespace]:
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = raw.get("reference_namespaces") if isinstance(raw, dict) else None
    namespaces: dict[str, _Namespace] = {}
    for name, spec in (block or {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"{contract_path}: reference namespace {name!r} malformed")
        namespaces[str(name)] = _Namespace(
            folder=_folder(str(spec["folder"])),
            key_template=str(spec["key_template"]),
        )
    return namespaces


def _folder(folder: str) -> str:
    """A validated absolute folder with no trailing slash (``/`` for the root)."""
    if not folder.startswith("/"):
        raise _InvalidRequestError("a folder is absolute: it starts with /")
    segments = [s for s in folder.split("/") if s]
    if any(not _SEGMENT.match(s) or s in (".", "..") for s in segments):
        raise _InvalidRequestError("a folder segment is letters, digits, _ . or -")
    return "/" + "/".join(segments)


def _join(root: str, folder: str) -> str:
    if folder == "/":
        return root
    return folder if root == "/" else f"{root}{folder}"


async def http_exchange(
    method: str,
    url: str,
    *,
    params: Mapping[str, str] | None,
    payload: Mapping[str, object] | None,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    """The node's contract-declared HTTP transport: one request, (status, JSON object).

    The store adapters are handed this and open no connection themselves. A
    transport failure raises ``ConnectionError`` naming only the exception
    type; the body is parsed for a JSON object and never surfaced otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.request(
                method,
                url,
                params=dict(params) if params is not None else None,
                json=dict(payload) if payload is not None else None,
                headers=dict(headers),
            )
    except httpx.HTTPError as exc:
        raise ConnectionError(type(exc).__name__) from None
    try:
        body = response.json() if response.content.strip() else {}
    except ValueError:
        body = {}
    return response.status_code, body if isinstance(body, dict) else {}


def _safe_detail(exc: BaseException) -> str:
    message = str(exc)
    if _SAFE_MESSAGE.match(message):
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


class HandlerSecretStore:
    """EFFECT: one resolve, create-only put, or folder listing per request.

    Args:
        store: The store to use. With none, the overlay's provider selects one
            from the node's adapters package on the first request.
        overlay: The store addressing. With none, it is read from
            ``<onex_home>/config.yaml``.
        onex_home: The ONEX home holding ``config.yaml`` and
            ``credentials.json``. Defaults to the standard one.
        bootstrap_store: Where the identity's credential references resolve.
            With none, ``<onex_home>/credentials.json``.
        contract_path: The node contract, for ``reference_namespaces``.
    """

    def __init__(
        self,
        *,
        store: ProtocolSecretStore | None = None,
        overlay: ModelSecretStoreOverlay | None = None,
        onex_home: Path | None = None,
        bootstrap_store: ProtocolSecretStore | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._store = store
        self._overlay = overlay
        self._onex_home = onex_home
        self._bootstrap_store = bootstrap_store
        self._namespaces = _load_namespaces(contract_path or _CONTRACT_PATH)

    # ------------------------------------------------------------------ naming

    def _parse_reference(self, reference: str) -> _Target:
        if not reference.startswith(_REFERENCE_PREFIX):
            raise _InvalidRequestError("a reference starts with secret://onex/")
        segments = reference[len(_REFERENCE_PREFIX) :].split("/")
        if len(segments) < 1 or any(
            not _SEGMENT.match(s) or s in (".", "..") for s in segments
        ):
            raise _InvalidRequestError(
                "a reference segment is letters, digits, _ . or -"
            )
        *folders, name = segments
        namespace = self._namespaces.get(folders[0]) if folders else None
        if namespace is not None:
            rest = "/".join(folders[1:])
            folder = (
                namespace.folder if not rest else _join(namespace.folder, f"/{rest}")
            )
            return _Target(folder=folder, key=namespace.key_template.format(name=name))
        return _Target(folder=_folder("/" + "/".join(folders)), key=name)

    def _target(self, request: ModelSecretStoreRequest) -> _Target:
        if request.operation is EnumSecretStoreOperation.LIST:
            if request.reference is not None or request.key is not None:
                raise _InvalidRequestError("list takes a folder only")
            return _Target(folder=_folder(request.folder or "/"), key=None)
        if (request.reference is None) == (request.key is None):
            raise _InvalidRequestError("name exactly one of reference or key")
        if request.reference is not None:
            if request.folder is not None:
                raise _InvalidRequestError("a reference carries its own folder")
            return self._parse_reference(request.reference)
        key = request.key or ""
        if not _SEGMENT.match(key):
            raise _InvalidRequestError("a key is letters, digits, _ . or -")
        return _Target(folder=_folder(request.folder or "/"), key=key)

    # ---------------------------------------------------------------- the store

    async def _credential(
        self, ref: str, files: StoreOnexHomeFiles
    ) -> SecretStr | None:
        if self._bootstrap_store is not None:
            value = await self._bootstrap_store.get_secret(ref)
            return SecretStr(value) if value else None
        remediation = f"file it in credentials.json under '{ref}' (mode 0600)"
        return SecretStr(files.read_secret(ref, remediation))

    async def _resolve_store(self) -> _Resolved:
        """The store and the root folder, or why there is none."""
        from omnibase_core.errors.model_onex_error import ModelOnexError
        from omnibase_infra.cli.store_onex_home_files import StoreOnexHomeFiles
        from omnibase_infra.doctor.delegation_doctor_support import default_onex_home

        misconfigured = EnumSecretStoreOutcome.MISCONFIGURED
        files = StoreOnexHomeFiles(self._onex_home or default_onex_home())
        overlay = self._overlay
        if overlay is None:
            try:
                overlay = load_secret_store_overlay(
                    files.load_config(must_exist=False), source=str(files.config_path)
                )
            except (ModelOnexError, ValueError) as exc:
                return _Resolved(refusal=misconfigured, detail=_detail_of(exc))
            if overlay is None:
                return _Resolved(
                    refusal=EnumSecretStoreOutcome.NOT_CONFIGURED,
                    detail=f"no secret_store block in {files.config_path}",
                )
            self._overlay = overlay
        try:
            root = _folder(overlay.root_folder)
        except _InvalidRequestError as exc:
            return _Resolved(refusal=misconfigured, detail=f"root_folder: {exc}")
        if self._store is not None:
            return _Resolved(store=self._store, root=root)
        try:
            client_id = await self._credential(overlay.client_id_ref, files)
            client_secret = await self._credential(overlay.client_secret_ref, files)
        except ModelOnexError as exc:
            return _Resolved(refusal=misconfigured, detail=_detail_of(exc))
        if client_id is None or client_secret is None:
            missing = (
                overlay.client_id_ref
                if client_id is None
                else overlay.client_secret_ref
            )
            return _Resolved(
                refusal=misconfigured,
                detail=f"the bootstrap store has no entry for '{missing}'",
            )
        store = secret_store_for(
            overlay,
            client_id=client_id,
            client_secret=client_secret,
            exchange=http_exchange,
        )
        if store is None:
            return _Resolved(
                refusal=misconfigured,
                detail=f"no adapter for provider '{overlay.provider}'",
            )
        self._store = store
        return _Resolved(store=store, root=root)

    # ------------------------------------------------------------------ handle

    async def handle(self, request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        def result(
            outcome: EnumSecretStoreOutcome,
            *,
            folder: str | None = None,
            key: str | None = None,
            keys: tuple[str, ...] = (),
            value: SecretStr | None = None,
            detail: str | None = None,
        ) -> ModelSecretStoreResult:
            logger.info(
                "secret store %s folder=%s key=%s outcome=%s%s",
                request.operation.value,
                folder,
                key,
                outcome.value,
                f" detail={detail}" if detail else "",
            )
            return ModelSecretStoreResult(
                operation=request.operation,
                outcome=outcome,
                folder=folder,
                key=key,
                keys=keys,
                value=value,
                detail=detail,
                correlation_id=request.correlation_id,
            )

        try:
            target = self._target(request)
        except _InvalidRequestError as exc:
            return result(EnumSecretStoreOutcome.INVALID_REQUEST, detail=str(exc))
        if (
            request.operation is EnumSecretStoreOperation.CREATE
            and request.value is None
        ):
            return result(
                EnumSecretStoreOutcome.VALUE_MISSING,
                folder=target.folder,
                key=target.key,
                detail="a create carries its value in process only, never as an event",
            )

        resolved = await self._resolve_store()
        store = resolved.store
        if store is None:
            return result(
                resolved.refusal or EnumSecretStoreOutcome.MISCONFIGURED,
                detail=resolved.detail,
            )
        folder = _join(resolved.root, target.folder)
        key = target.key

        try:
            if request.operation is EnumSecretStoreOperation.LIST:
                found = await store.list_keys(prefix=folder)
                base = folder.rstrip("/")
                names = tuple(
                    sorted(
                        k.rpartition("/")[2]
                        for k in found
                        if k.rpartition("/")[0] == base
                    )
                )
                return result(EnumSecretStoreOutcome.LISTED, folder=folder, keys=names)
            full_key = f"{folder.rstrip('/')}/{key}"
            if request.operation is EnumSecretStoreOperation.RESOLVE:
                value = await store.get_secret(full_key)
                if value is None:
                    return result(
                        EnumSecretStoreOutcome.NOT_FOUND, folder=folder, key=key
                    )
                return result(
                    EnumSecretStoreOutcome.RESOLVED,
                    folder=folder,
                    key=key,
                    value=SecretStr(value),
                )
            secret = request.value or SecretStr("")  # VALUE_MISSING answered above
            created = await store.set_secret(full_key, secret.get_secret_value())
            return result(
                EnumSecretStoreOutcome.CREATED
                if created
                else EnumSecretStoreOutcome.EXISTS,
                folder=folder,
                key=key,
            )
        except PermissionError as exc:
            outcome = EnumSecretStoreOutcome.REFUSED
            detail = _safe_detail(exc)
        except (ConnectionError, TimeoutError, OSError) as exc:
            outcome = EnumSecretStoreOutcome.UNREACHABLE
            detail = _safe_detail(exc)
        except Exception as exc:
            outcome = EnumSecretStoreOutcome.FAILED
            detail = _safe_detail(exc)
        return result(outcome, folder=folder, key=key, detail=detail)


def _detail_of(exc: BaseException) -> str:
    """A configuration fault's message: it names files and keys, never values."""
    message = getattr(exc, "message", None)
    return str(message) if isinstance(message, str) else str(exc)


__all__ = ["HandlerSecretStore", "http_exchange"]
