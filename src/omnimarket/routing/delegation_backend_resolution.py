# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical routing-authority resolve step for delegation backend selection.

``HandlerLlmDelegationCall`` REQUIRES a resolved ``model_id`` + ``endpoint_ref``
as inputs — it executes exactly one LLM call and never resolves backend authority
itself. This module is the routing-authority home that resolves those two values
from the bifrost delegation contract (``bifrost_delegation.yaml`` + the installer
overlay) BEFORE the effect handler is invoked.

It replaces the hand-rolled ``_load_bifrost_config`` / ``_select_backend`` that
previously lived inside ``port_direct_curl_dispatch`` (OMN-13160). No config
loading lives in a port anymore; the orchestrator calls this resolve step and
hands the result to the effect handler.

OMN-12815 / OMN-13159: every resolved ``endpoint_url`` is the COMPLETE final URL
(incl. the full ``/v1/chat/completions`` path). It is carried verbatim into the
effect handler's ``endpoint_ref`` and posted with no construction. A bare base or
non-http(s) value fails closed downstream — this resolver does not construct
paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"
_BIFROST_CONFIG_PATH = _CONFIGS_DIR / "bifrost_delegation.yaml"
_OVERLAY_PATH = Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"


class ModelResolvedDelegationBackend(BaseModel):
    """Routing-authority resolution of one delegation backend for a task type.

    The two load-bearing fields the effect handler requires are ``model_id`` and
    ``endpoint_ref`` (the COMPLETE endpoint URL, carried verbatim). ``backend_id``
    and ``tier`` are provenance carried for telemetry and inference-protocol
    shaping; ``provider_request_options`` and ``extra_headers`` are the outbound
    request-shaping carried from the inference protocol config + backend config.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    model_id: str = Field(..., min_length=1)
    endpoint_ref: str = Field(
        ...,
        min_length=1,
        description=(
            "COMPLETE resolved chat-completions URL, posted verbatim by the "
            "effect handler (OMN-12815/OMN-13159)."
        ),
    )
    tier: str = Field(default="unknown")
    max_tokens: int = Field(
        ...,
        ge=1,
        description=(
            "Per-backend output-token budget/ceiling resolved from the routing "
            "contract (overlay-overridable). The orchestrator uses this as the "
            "effective max_tokens when the request omits one and as the hard cap "
            "when the request supplies an explicit value (OMN-13161)."
        ),
    )
    timeout_ms: int = Field(
        ...,
        ge=1,
        description=(
            "Per-backend HTTP request timeout in milliseconds resolved from the "
            "routing contract (overlay-overridable). The orchestrator threads this "
            "(÷1000) into the effect handler's transport so large generations are "
            "not capped by a hardcoded transport default (OMN-13170)."
        ),
    )
    extra_headers: dict[str, str] = Field(default_factory=dict)
    secret_ref: str | None = Field(
        default=None,
        description=(
            "Logical secret reference (e.g. ``llm.glm.api_key``) the effect "
            "boundary resolves to the literal API key via ProtocolSecretStore "
            "(OMN-12824). Only the reference name is carried here; the value is "
            "never resolved in the routing authority."
        ),
    )


def load_bifrost_backends(
    *,
    config_path: Path = _BIFROST_CONFIG_PATH,
    overlay_path: Path = _OVERLAY_PATH,
) -> list[dict[str, Any]]:
    """Load and merge bifrost_delegation.yaml with the installer overlay.

    The overlay (usually ``~/.omninode/delegation/bifrost_overrides.yaml``)
    supplies COMPLETE endpoint URLs for site-specific local backends that are
    null in the committed repo default. Overlay entries are merged onto matching
    ``backend_id`` entries field-by-field.
    """
    backends: list[dict[str, Any]] = []
    if config_path.is_file():
        base = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        backends = list(base.get("backends", []))

    if overlay_path.is_file():
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        overlay_backends = {b["backend_id"]: b for b in overlay.get("backends", [])}
        for i, backend in enumerate(backends):
            override = overlay_backends.get(backend["backend_id"])
            if override is not None:
                backends[i] = {**backend, **override}

    return backends


def _select_backend(
    backends: list[dict[str, Any]], task_type: str
) -> dict[str, Any] | None:
    """Select the best backend for ``task_type`` from the merged config.

    Prefers a backend whose ``capabilities`` or ``use_for`` lists the task type
    and that has a populated ``endpoint_url``; otherwise falls back to the first
    backend with any populated ``endpoint_url``.
    """
    for backend in backends:
        if not backend.get("endpoint_url"):
            continue
        capabilities = backend.get("capabilities", [])
        use_for = backend.get("use_for", [])
        if task_type in capabilities or task_type in use_for:
            return backend
    for backend in backends:
        if backend.get("endpoint_url"):
            return backend
    return None


def _select_backend_by_id(
    backends: list[dict[str, Any]], backend_id: str
) -> dict[str, Any] | None:
    """Select the backend with ``backend_id`` that has a populated endpoint_url."""
    for backend in backends:
        if backend.get("backend_id") == backend_id and backend.get("endpoint_url"):
            return backend
    return None


def resolve_delegation_backend(
    task_type: str,
    *,
    backend_id: str | None = None,
    backends: list[dict[str, Any]] | None = None,
    config_path: Path = _BIFROST_CONFIG_PATH,
    overlay_path: Path = _OVERLAY_PATH,
) -> ModelResolvedDelegationBackend:
    """Resolve ``model_id`` + ``endpoint_ref`` for ``task_type`` from bifrost.

    When ``backend_id`` is supplied the resolver targets that exact backend (it
    must carry a populated COMPLETE ``endpoint_url``) instead of selecting by
    ``task_type`` capability. This is the path the LLM-judge uses to pin a
    concrete cloud backend with a committed verbatim endpoint URL — it never
    passes a TIER name to the inference layer (OMN-13470).

    Fails closed when no backend carries a populated ``endpoint_url`` — the
    installer overlay is responsible for supplying COMPLETE local endpoint URLs.
    """
    merged = (
        backends
        if backends is not None
        else load_bifrost_backends(config_path=config_path, overlay_path=overlay_path)
    )
    if backend_id is not None:
        backend = _select_backend_by_id(merged, backend_id)
        if backend is None:
            raise RuntimeError(
                f"No delegation backend {backend_id!r} with a populated "
                "endpoint_url found in the bifrost config. Declare a COMPLETE "
                "endpoint_url for it in the committed config or the overlay "
                f"({overlay_path})."
            )
    else:
        backend = _select_backend(merged, task_type)
        if backend is None:
            raise RuntimeError(
                "No delegation backend with a populated endpoint_url found in the "
                "bifrost config. Supply COMPLETE local endpoint URLs in the overlay "
                f"({overlay_path})."
            )

    endpoint_url = backend.get("endpoint_url")
    if not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} resolved a non-string or "
            "empty endpoint_url; the overlay must supply the COMPLETE URL."
        )

    model_name = backend.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} has no model_name; the "
            "overlay must resolve the model identifier at deploy time."
        )

    # OMN-13161: the per-backend output-token budget is contract-resolved — there
    # is no Python constant or env-var fallback for the value. A backend that omits
    # max_tokens (or sets a non-positive one) fails closed rather than silently
    # falling back to a magic number.
    raw_max_tokens = backend.get("max_tokens")
    if not isinstance(raw_max_tokens, int) or isinstance(raw_max_tokens, bool):
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} has no integer max_tokens; the "
            "routing contract (bifrost_delegation.yaml + overlay) must declare a "
            "per-backend output-token budget (OMN-13161)."
        )
    if raw_max_tokens < 1:
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} declares max_tokens="
            f"{raw_max_tokens}; the per-backend output-token budget must be >= 1."
        )

    # OMN-13170: the per-backend HTTP timeout is contract-resolved — there is no
    # Python constant or transport default that silently overrides it. A backend
    # that omits timeout_ms (or sets a non-positive one) fails closed rather than
    # falling back to the old hardcoded 120s transport cap.
    raw_timeout_ms = backend.get("timeout_ms")
    if not isinstance(raw_timeout_ms, int) or isinstance(raw_timeout_ms, bool):
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} has no integer timeout_ms; the "
            "routing contract (bifrost_delegation.yaml + overlay) must declare a "
            "per-backend HTTP timeout (OMN-13170)."
        )
    if raw_timeout_ms < 1:
        raise RuntimeError(
            f"backend {backend.get('backend_id')!r} declares timeout_ms="
            f"{raw_timeout_ms}; the per-backend HTTP timeout must be >= 1."
        )

    raw_headers = backend.get("extra_headers") or {}
    extra_headers = {str(k): str(v) for k, v in raw_headers.items()}

    raw_secret_ref = backend.get("secret_ref")
    secret_ref = (
        str(raw_secret_ref)
        if isinstance(raw_secret_ref, str) and raw_secret_ref.strip()
        else None
    )

    return ModelResolvedDelegationBackend(
        backend_id=str(backend["backend_id"]),
        model_id=model_name,
        endpoint_ref=endpoint_url,
        tier=str(backend.get("tier", "unknown")),
        max_tokens=raw_max_tokens,
        timeout_ms=raw_timeout_ms,
        extra_headers=extra_headers,
        secret_ref=secret_ref,
    )


def resolve_effective_max_tokens(
    *, requested: int | None, backend_max_tokens: int
) -> int:
    """Resolve the effective output-token budget for one delegation call.

    The per-backend ``backend_max_tokens`` is the contract-resolved ceiling
    (OMN-13161). When the request omits ``max_tokens`` the backend value is used
    verbatim; when it supplies an explicit value the result is capped at the
    backend ceiling (``min(requested, backend_max_tokens)``) so a caller can ask
    for fewer tokens but never more than the backend allows.
    """
    if backend_max_tokens < 1:
        raise ValueError(f"backend_max_tokens must be >= 1, got {backend_max_tokens}")
    if requested is None:
        return backend_max_tokens
    if requested < 1:
        raise ValueError(f"requested max_tokens must be >= 1, got {requested}")
    return min(requested, backend_max_tokens)


def resolve_timeout_seconds(*, backend_timeout_ms: int) -> float:
    """Convert the contract-resolved per-backend timeout (ms) to seconds.

    The per-backend ``backend_timeout_ms`` is the contract-resolved HTTP timeout
    (OMN-13170). The effect handler's transport takes seconds, so the orchestrator
    converts ms → seconds here. Fails closed on a non-positive value rather than
    falling back to the old hardcoded 120s transport cap.
    """
    if backend_timeout_ms < 1:
        raise ValueError(f"backend_timeout_ms must be >= 1, got {backend_timeout_ms}")
    return backend_timeout_ms / 1000.0


__all__ = [
    "ModelResolvedDelegationBackend",
    "load_bifrost_backends",
    "resolve_delegation_backend",
    "resolve_effective_max_tokens",
    "resolve_timeout_seconds",
]
