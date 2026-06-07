# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve an LLM endpoint from the bifrost routing authority by backend ref.

Any node that calls an LLM (delegation, generation, ...) resolves its
endpoint_url + served model + api_key_ref PER backend from the bifrost
contract + overlay — never from a shared ``LLM_*_URL`` env var. Each backend's
``endpoint_url`` carries its own provider-correct full URL and path convention
(vLLM ``/v1/chat/completions``, a cloud provider's own URL, etc.), so there is
no single base URL with per-mode path appending.

This module is the shared resolver: it reads the same bifrost authority the
delegation router uses (``load_bifrost_delegation_config``) and returns the
resolved endpoint for a single ``backend_id``. It fails closed — a missing
backend or an unconfigured endpoint raises, never silently defaults.

Related:
    - OMN-12779: generation routing authority (provider/model/endpoint from contract)
    - OMN-12802: delete the shared LLM_CODER_URL env stopgap from generation
    - OMN-10657: endpoint resolution from bifrost contract, not env vars
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)

# Infra config-file path overrides (not model config) — mirror the delegation
# router so tests/staging can point the resolver at a non-default bifrost
# contract or overlay. These name FILE PATHS, never an endpoint URL.
_BIFROST_CONTRACT_PATH_ENV = (
    "BIFROST_CONTRACT_PATH"  # ONEX_EXCLUDE: contract path override
)
_BIFROST_OVERLAY_PATH_ENV = (
    "BIFROST_OVERLAY_PATH"  # ONEX_EXCLUDE: overlay path override
)


class ModelResolvedEndpoint(BaseModel):
    """A backend endpoint resolved from the bifrost routing authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(
        ..., description="The bifrost backend_id that was resolved."
    )
    endpoint_url: str = Field(
        ...,
        description=(
            "Full provider-correct URL to POST to (already carries the path "
            "convention, e.g. /v1/chat/completions). Never a bare base."
        ),
    )
    model_name: str = Field(
        ..., description="The served model name declared by the backend entry."
    )
    api_key_ref: str | None = Field(
        default=None,
        description="Name of the env var holding the API key (reference, not value).",
    )
    timeout_ms: int = Field(..., ge=0, description="Backend request timeout (ms).")


def resolve_endpoint(
    backend_ref: str,
    *,
    config_path: Path | None = None,
    overlay_path: Path | None = None,
) -> ModelResolvedEndpoint:
    """Resolve a single backend's endpoint from the bifrost routing authority.

    Args:
        backend_ref: The bifrost ``backend_id`` to resolve (e.g. ``"local-coder"``).
            This is the ``model_routing.endpoint_ref`` declared by the calling
            node's contract.
        config_path: Optional bifrost config override. Defaults to the
            ``BIFROST_CONTRACT_PATH`` env (a FILE PATH, not a URL) when set, then
            to the canonical repo config — same precedence the delegation router
            uses.
        overlay_path: Optional endpoint overlay override. Defaults to the
            ``BIFROST_OVERLAY_PATH`` env (a FILE PATH) when set, then to the
            canonical ``~/.omninode/delegation/bifrost_overrides.yaml``.

    Returns:
        The resolved endpoint with a full, provider-correct ``endpoint_url``.

    Raises:
        ValueError: If ``backend_ref`` is not declared in the routing authority,
            or its ``endpoint_url`` / ``model_name`` is not configured. Fails
            closed — no silent default endpoint.
    """
    if not backend_ref:
        raise ValueError(
            "resolve_endpoint requires a non-empty backend_ref "
            "(contract model_routing.endpoint_ref)"
        )

    if config_path is None:
        env_config = os.environ.get(_BIFROST_CONTRACT_PATH_ENV, "").strip()
        config_path = Path(env_config) if env_config else None
    if overlay_path is None:
        env_overlay = os.environ.get(_BIFROST_OVERLAY_PATH_ENV, "").strip()
        overlay_path = Path(env_overlay) if env_overlay else None

    config = load_bifrost_delegation_config(
        config_path=config_path,
        overlay_path=overlay_path,
    )

    backend = next(
        (b for b in config.backends if b.backend_id == backend_ref),
        None,
    )
    if backend is None:
        declared = sorted(b.backend_id for b in config.backends)
        raise ValueError(
            f"backend_ref {backend_ref!r} is not declared in the bifrost routing "
            f"authority; declared backends: {declared}"
        )

    endpoint_url = (backend.endpoint_url or "").strip()
    if not endpoint_url:
        raise ValueError(
            f"backend {backend_ref!r} has no configured endpoint_url; populate it "
            "in the bifrost overlay (bifrost_overrides.yaml) — no env fallback, "
            "no silent default"
        )

    model_name = (backend.model_name or "").strip()
    if not model_name:
        raise ValueError(
            f"backend {backend_ref!r} has no configured model_name; populate it "
            "in the bifrost contract/overlay"
        )

    api_key_ref = backend.api_key_env or None

    return ModelResolvedEndpoint(
        backend_id=backend_ref,
        endpoint_url=endpoint_url,
        model_name=model_name,
        api_key_ref=api_key_ref,
        timeout_ms=backend.timeout_ms,
    )


__all__: list[str] = ["ModelResolvedEndpoint", "resolve_endpoint"]
