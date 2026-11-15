# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The organization Actions secret requests of node_github_repo_write_effect.

``org_secret_set`` reads one value from the secret store by the reference the overlay
declares, seals it with a libsodium sealed box to the organization's public key and
sends it with one PUT to ``/orgs/{org}/actions/secrets/{name}``. Only the
``/orgs/{org}/actions/secrets`` endpoints and the repository-id read are called.

The value has no field on any request, result or event. It is held in one local
variable in the handler, sealed here, and dropped. The sealed form goes only into the
PUT: the request the result records is ``recorded_put_request``, which carries
``SEALED_PLACEHOLDER`` where the sealed value was. Every error raised here carries
fixed text, never the value, the sealed form, a key or a store message.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Protocol

from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox
from pydantic import BaseModel, ConfigDict, SecretStr

from omnimarket.github_landing.github_landing_requests import split_repository
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.node_github_repo_write_effect.models import EnumRepoWriteRefusal
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelOrgSecretSetEntry,
    ModelRepoWriteOverlay,
)

#: What the recorded PUT request carries where the sealed value was sent.
SEALED_PLACEHOLDER = "<sealed value withheld>"

#: Reads the value the secret store holds under a reference; None when it holds none.
OrgSecretReader = Callable[[str], Awaitable[SecretStr | None]]


class OrgSecretSourceError(Exception):
    """The secret store could not give the value. Fixed text; no cause is chained."""


class OrgSecretSealError(Exception):
    """The value could not be sealed to the key. Fixed text; no cause is chained."""


class ModelOrgSecretPlan(BaseModel):
    """What the overlay allows for one org_secret_set: the entry, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry: ModelOrgSecretSetEntry


def organization_of(repository: str) -> str:
    """The organization an org secret request acts on: the owner part of ``repository``."""
    return split_repository(repository)[0]


def org_secret_plan(
    overlay: ModelRepoWriteOverlay | None, repository: str, name: str
) -> ModelOrgSecretPlan | None:
    """The overlay entry for (the repository's organization, ``name``), else None.

    None means the name is not allowed: no overlay, no ``org_secret_sets`` key or no
    entry for that pair. The caller refuses before any store read or GitHub call.
    """
    if overlay is None or overlay.org_secret_sets is None:
        return None
    organization = organization_of(repository).lower()
    for entry in overlay.org_secret_sets.entries:
        if entry.organization.lower() == organization and entry.name == name:
            return ModelOrgSecretPlan(entry=entry)
    return None


def _secrets_path(organization: str) -> str:
    return f"/orgs/{urllib.parse.quote(organization, safe='')}/actions/secrets"


def org_public_key_request(organization: str) -> ModelGithubHttpRequest:
    """GET the organization's Actions secret public key and its key id."""
    return ModelGithubHttpRequest(
        method="GET", path=f"{_secrets_path(organization)}/public-key"
    )


def repository_request(repository: str) -> ModelGithubHttpRequest:
    """GET one repository, for the numeric id ``selected_repository_ids`` takes."""
    owner, name = split_repository(repository)
    return ModelGithubHttpRequest(method="GET", path=f"/repos/{owner}/{name}")


def _put_body(
    entry: ModelOrgSecretSetEntry,
    encrypted_value: str,
    key_id: str,
    repository_ids: tuple[object, ...],
) -> dict[str, object]:
    body: dict[str, object] = {
        "encrypted_value": encrypted_value,
        "key_id": key_id,
        "visibility": entry.visibility,
    }
    if entry.visibility == "selected":
        body["selected_repository_ids"] = list(repository_ids)
    return body


def put_secret_request(
    entry: ModelOrgSecretSetEntry,
    encrypted_value: str,
    key_id: str,
    repository_ids: tuple[int, ...],
) -> ModelGithubHttpRequest:
    """PUT the sealed value; the one request that carries it, never recorded."""
    return ModelGithubHttpRequest(
        method="PUT",
        path=f"{_secrets_path(entry.organization)}/{urllib.parse.quote(entry.name, safe='')}",
        body=_put_body(entry, encrypted_value, key_id, repository_ids),
    )


def recorded_put_request(request: ModelGithubHttpRequest) -> ModelGithubHttpRequest:
    """The PUT as a result records it: the sealed value replaced by a placeholder."""
    body = dict(request.body or {})
    if "encrypted_value" in body:
        body["encrypted_value"] = SEALED_PLACEHOLDER
    return ModelGithubHttpRequest(method=request.method, path=request.path, body=body)


def planned_org_secret_requests(
    entry: ModelOrgSecretSetEntry,
) -> tuple[ModelGithubHttpRequest, ...]:
    """The calls a dry_run plans: the key read, each repository read and the PUT."""
    reads = [org_public_key_request(entry.organization)]
    reads.extend(repository_request(repository) for repository in entry.repositories)
    placeholders = tuple(f"<id of {repository}>" for repository in entry.repositories)
    put = ModelGithubHttpRequest(
        method="PUT",
        path=f"{_secrets_path(entry.organization)}/{urllib.parse.quote(entry.name, safe='')}",
        body=_put_body(
            entry, SEALED_PLACEHOLDER, "<key id read from GitHub>", placeholders
        ),
    )
    return (*reads, put)


def seal_value(public_key_b64: str, value: str) -> str:
    """Seal ``value`` to the base64 public key with a libsodium sealed box; base64 out."""
    try:
        box = SealedBox(PublicKey(base64.b64decode(public_key_b64, validate=True)))
        sealed = box.encrypt(value.encode("utf-8"))
    except (binascii.Error, ValueError, TypeError, CryptoError):
        raise OrgSecretSealError(
            "the organization public key could not seal the value"
        ) from None
    return base64.b64encode(sealed).decode("ascii")


def org_secret_value_digest(salt: bytes, name: str, value: str) -> str:
    """A keyed digest of ``value`` for the replay record; the value never leaves it.

    The idempotency record keeps this beside a completed org_secret_set so a replay can
    tell a rotated value (digest differs, the PUT is sent) from an unchanged one (digest
    matches, nothing is sent). The key is a per-handler random salt, so the digest is not
    a bare hash of the value and cannot be matched offline. It is held in process memory
    only: never logged, never put on a result, event or error, and never printed beside
    the value.
    """
    return hmac.new(salt, f"{name}\0{value}".encode(), hashlib.sha256).hexdigest()


async def read_org_secret_value(ref: str) -> SecretStr | None:
    """Read the value stored under ``ref`` through the secret store resolver.

    Absence returns None. Any store or SDK failure raises ``OrgSecretSourceError`` with
    fixed text: such an error can carry a secret, so neither its message nor its cause
    is forwarded.
    """
    try:
        return await resolve_api_key_async(ref, required=False)
    except Exception:
        raise OrgSecretSourceError("the secret store could not be read") from None


class OrgSecretSend(Protocol):
    """The handler's send: one request out, the response back, refusing on a non-2xx."""

    async def __call__(
        self,
        request: ModelGithubHttpRequest,
        *,
        recorded: ModelGithubHttpRequest | None = None,
    ) -> ModelGithubHttpResponse: ...


def _text_at(body: dict[str, object] | None, key: str) -> str | None:
    value = (body or {}).get(key)
    return value if isinstance(value, str) and value else None


async def apply_org_secret(
    plan: ModelOrgSecretPlan,
    *,
    send: OrgSecretSend,
    refuse: Callable[[EnumRepoWriteRefusal, str], Exception],
    read_value: OrgSecretReader,
) -> str:
    """Set the planned org secret and return the key id the value was sealed to.

    The value is read from the store first, so an unreadable store refuses before any
    GitHub call, held in one local variable, sealed and dropped before the PUT. Every
    refusal text here is fixed: none carries the value, its sealed form or a store message.
    """
    entry = plan.entry
    try:
        stored = await read_value(entry.secret_ref)
    except OrgSecretSourceError:
        raise refuse(
            EnumRepoWriteRefusal.ORG_SECRET_SOURCE_UNREADABLE,
            f"the secret store could not be read for org secret {entry.name}; no call made",
        ) from None
    if stored is None:
        raise refuse(
            EnumRepoWriteRefusal.ORG_SECRET_SOURCE_UNREADABLE,
            f"the secret store holds no value for org secret {entry.name}; no call made",
        )
    key_body = (await send(org_public_key_request(entry.organization))).body
    public_key, key_id = _text_at(key_body, "key"), _text_at(key_body, "key_id")
    if public_key is None or key_id is None:
        raise refuse(
            EnumRepoWriteRefusal.GITHUB_REFUSED,
            f"the public key of {entry.organization} came back without key and key_id",
        )
    repository_ids: list[int] = []
    for repository in entry.repositories:
        repository_id = ((await send(repository_request(repository))).body or {}).get(
            "id"
        )
        if not isinstance(repository_id, int) or isinstance(repository_id, bool):
            raise refuse(
                EnumRepoWriteRefusal.GITHUB_REFUSED,
                f"repository {repository} came back without a numeric id",
            )
        repository_ids.append(repository_id)
    try:
        sealed = seal_value(public_key, stored.get_secret_value())
    except OrgSecretSealError:
        raise refuse(
            EnumRepoWriteRefusal.GITHUB_REFUSED,
            f"the public key of {entry.organization} could not seal the value; no write sent",
        ) from None
    finally:
        del stored
    put = put_secret_request(entry, sealed, key_id, tuple(repository_ids))
    await send(put, recorded=recorded_put_request(put))
    return key_id
