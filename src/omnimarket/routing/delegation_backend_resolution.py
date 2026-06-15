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
    extra_headers: dict[str, str] = Field(default_factory=dict)


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


def resolve_delegation_backend(
    task_type: str,
    *,
    backends: list[dict[str, Any]] | None = None,
    config_path: Path = _BIFROST_CONFIG_PATH,
    overlay_path: Path = _OVERLAY_PATH,
) -> ModelResolvedDelegationBackend:
    """Resolve ``model_id`` + ``endpoint_ref`` for ``task_type`` from bifrost.

    Fails closed when no backend carries a populated ``endpoint_url`` — the
    installer overlay is responsible for supplying COMPLETE local endpoint URLs.
    """
    merged = (
        backends
        if backends is not None
        else load_bifrost_backends(config_path=config_path, overlay_path=overlay_path)
    )
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

    raw_headers = backend.get("extra_headers") or {}
    extra_headers = {str(k): str(v) for k, v in raw_headers.items()}

    return ModelResolvedDelegationBackend(
        backend_id=str(backend["backend_id"]),
        model_id=model_name,
        endpoint_ref=endpoint_url,
        tier=str(backend.get("tier", "unknown")),
        extra_headers=extra_headers,
    )


__all__ = [
    "ModelResolvedDelegationBackend",
    "load_bifrost_backends",
    "resolve_delegation_backend",
]
