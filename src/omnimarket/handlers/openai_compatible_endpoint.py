# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An endpoint that speaks the OpenAI API and needs a key, by its URL (OMN-21263).

The keyed providers are catalogue rows with fixed URLs. One generic row,
``openai_compatible``, declares ``endpoint_from_registration: true``: the
developer names the endpoint's base URL when they register the key, and its
chat and model-list URLs are the OpenAI API's own paths under it. The row's own
URLs are placeholders that never route.

The base URL is stored beside the credential, in a nullable ``base_url`` column
of the local credential table, and read back by the routing half, the effect
boundary and the key's model checks. It is a URL, never a secret.

While ``onex models add`` registers such a key, :func:`registering_endpoint`
holds its URL, so the shared key path checks models at that URL and the local
secret store records it with the credential. Storing a key for this row any
other way (``onex secret set llm.openai_compatible.api_key``) has no URL and is
refused by the store.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlparse

from omnimarket.handlers.openai_compatible_endpoint_adapter import (
    read_base_url,
    record_base_url,
)
from omnimarket.routing.byok_provider_backends import (
    ModelByokProviderBackend,
    resolve_byok_provider_backend,
)

#: The generic row's provider id.
OPENAI_COMPATIBLE = "openai_compatible"
OPENAI_COMPATIBLE_HOW = (
    "Add an endpoint that speaks the OpenAI API and needs a key (vLLM or LM "
    "Studio behind auth, a company gateway, a hosted OpenAI-compatible service) "
    "with: onex models add openai_compatible --url <base url> --model <id>; "
    "the key is read from stdin or a hidden prompt"
)

_REGISTERING: ContextVar[str | None] = ContextVar(
    "openai_compatible_registering_url", default=None
)


class EndpointUrlRefusedError(ValueError):
    """The URL is missing, not http(s) with a host, or given for another provider."""


def is_generic(provider: str | None) -> bool:
    """Whether ``provider``'s catalogue row takes its endpoint at registration."""
    row = resolve_byok_provider_backend(provider) if provider else None
    return row is not None and row.endpoint_from_registration


def base_url_of(url: str) -> str:
    """``url`` without a trailing ``/`` or ``/v1``; refused unless http(s) with a host."""
    base = url.strip().rstrip("/")
    base = base.removesuffix("/v1")
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise EndpointUrlRefusedError(
            f"{url!r} is not an http(s) URL with a host, such as "
            "https://llm.example.com. Nothing was stored."
        )
    return base


def with_registered_base_url(
    backend: ModelByokProviderBackend, base_url: str
) -> ModelByokProviderBackend:
    """``backend`` addressing ``<base>/v1/chat/completions`` and ``<base>/v1/models``."""
    base = base_url.rstrip("/")
    return backend.model_copy(
        update={
            "endpoint_url": f"{base}/v1/chat/completions",
            "models_url": f"{base}/v1/models",
        }
    )


@contextmanager
def registering_endpoint(
    provider: str,
    url: str | None,
    refusal: Callable[[str], Exception] = EndpointUrlRefusedError,
) -> Iterator[str | None]:
    """Hold the base URL a key for ``provider`` is being registered with.

    Required for the generic row and refused, with ``refusal``, for any other
    provider. Yields the checked base URL, or ``None`` for a catalogue provider.
    """
    if not is_generic(provider):
        if url is not None:
            raise refusal(
                "--url is for a local model server or an OpenAI-compatible "
                f"endpoint, not {provider}. Nothing was stored."
            )
        yield None
        return
    if url is None or not url.strip():
        raise refusal(f"{OPENAI_COMPATIBLE_HOW}. Nothing was stored.")
    try:
        base = base_url_of(url)
    except EndpointUrlRefusedError as refused:
        raise refusal(str(refused)) from None
    token = _REGISTERING.set(base)
    try:
        yield _REGISTERING.get()
    finally:
        _REGISTERING.reset(token)


def registering_base_url() -> str | None:
    """The base URL :func:`registering_endpoint` holds, if a registration is running."""
    return _REGISTERING.get()


def stored_base_url(provider: str, *, db_path: Path | None = None) -> str | None:
    """The base URL stored with ``provider``'s credential, or ``None``."""
    found = read_base_url("provider", provider.strip().lower(), db_path=db_path)
    return found[1] if found is not None else None


def key_backend(
    provider: str, plan: str | None, *, db_path: Path | None = None
) -> ModelByokProviderBackend | None:
    """The catalogue row ``provider``'s key addresses, at its endpoint's URL.

    A catalogue provider's row is returned as it is. The generic row takes the
    URL a running registration holds, else the one stored with the key; with
    neither it has no endpoint and resolves to ``None``.
    """
    backend = resolve_byok_provider_backend(provider, plan=plan)
    if backend is None or not backend.endpoint_from_registration:
        return backend
    base = registering_base_url() or stored_base_url(provider, db_path=db_path)
    return with_registered_base_url(backend, base) if base else None


def registered_endpoint_url(
    backend: ModelByokProviderBackend, *, db_path: Path | None = None
) -> str:
    """The chat endpoint a key for ``backend`` posts to.

    The row's own for a catalogue provider. For the generic row, the URL stored
    with the key; a key stored with none keeps the row's placeholder, which
    resolves nowhere, so the route fails closed.
    """
    if not backend.endpoint_from_registration:
        return backend.endpoint_url
    base = stored_base_url(backend.provider, db_path=db_path)
    return (
        with_registered_base_url(backend, base).endpoint_url
        if base
        else backend.endpoint_url
    )


def registered_row_for_ref(
    secret_ref: str | None, endpoint_url: str, *, db_path: Path | None = None
) -> ModelByokProviderBackend | None:
    """The generic row a local credential ``secret_ref`` routes on, at ``endpoint_url``.

    The effect boundary holds a reference and an endpoint, not a provider. A
    reference with no stored base URL, or one whose URL is not this endpoint,
    resolves to ``None``.
    """
    found = (
        read_base_url("secret_ref", secret_ref, db_path=db_path) if secret_ref else None
    )
    if found is None:
        return None
    provider, base_url = found
    row = resolve_byok_provider_backend(provider)
    if row is None or not row.endpoint_from_registration:
        return None
    row = with_registered_base_url(row, base_url)
    return row if row.endpoint_url == endpoint_url else None


__all__ = [
    "OPENAI_COMPATIBLE",
    "OPENAI_COMPATIBLE_HOW",
    "EndpointUrlRefusedError",
    "base_url_of",
    "is_generic",
    "key_backend",
    "record_base_url",
    "registered_endpoint_url",
    "registered_row_for_ref",
    "registering_base_url",
    "registering_endpoint",
    "stored_base_url",
    "with_registered_base_url",
]
