# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pydantic models and loader for the LLM model registry (OMN-11779).

The model registry is the source of truth for model capabilities and pricing.
It is loaded at startup, validated, and its SHA-256 hash is included in every
delegation event for audit and reproducibility.

Hardcoding volatile model facts (pricing, context windows, model versions) in
Python or YAML outside this registry is prohibited (architectural invariant 3).
"""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from omnimarket.enums.enum_cost_basis import EnumCostBasis

# Default registry path — resolved relative to this file for portability
_DEFAULT_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "data"
    / "model_registry"
    / "model_registry_v1.yaml"
)


class ModelLlmModelProfile(BaseModel):
    """Profile for a single LLM model in the registry."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    model_id: str
    provider: str
    endpoint_env: str
    """Name of the env var that resolves to the endpoint URL. Never a raw URL."""

    context_window: int
    pricing_per_1m_input: Decimal
    pricing_per_1m_output: Decimal
    cost_basis: EnumCostBasis
    observed_at: str
    source: str
    """Source of pricing/capability data: 'provider docs', 'manual benchmark', 'openrouter catalog'."""

    model_name: str | None = None
    """Provider-specific model identifier (e.g. OpenRouter model slug).

    For a single-environment provider this is the served model id. For a provider
    served under DIFFERENT names per environment (e.g. Gemini via AI Studio vs
    Vertex), use ``served_model_names`` and leave this as the default/AI-Studio id.
    """

    served_model_names: dict[str, str] | None = None
    """Per-environment served model id mapping (OMN-12972).

    The registry key-path id (the YAML key / ``model_id``) is a STABLE routing
    handle. The provider-served wire name can differ per serving environment:
    Vertex serves the publisher-qualified ``publishers/google/models/<name>``
    while AI Studio (``generativelanguage``) serves the bare ``<name>``. Mapping
    each environment to its exact served name prevents the 404 that occurs when a
    single bare name is posted to the wrong environment (the OMN-12937 / P2.7
    failure: ``gemini-2.0-flash`` 404'd on Vertex). Keys are environment ids
    (e.g. ``ai_studio``, ``vertex``); values are the exact served model id for
    that environment.
    """

    requires_api_key_env: str | None = None
    """Name of env var for API key, if required."""

    requires_secret_ref: str | None = None
    """Logical secret-store reference (key NAME) for the backend bearer token,
    if required (e.g. ``llm.vertex.access_token`` for the Vertex ADC path). The
    token VALUE is resolved fail-closed at the effect boundary via the secret
    store and is never committed here — only the logical ref name (OMN-12971)."""

    notes: str | None = None

    @field_validator("context_window")
    @classmethod
    def context_window_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"context_window must be > 0, got {v}")
        return v

    @field_validator("served_model_names")
    @classmethod
    def served_model_names_non_empty(
        cls, v: dict[str, str] | None
    ) -> dict[str, str] | None:
        """Reject an empty mapping and any blank environment id / served name.

        A present-but-empty mapping is a configuration mistake (it silently
        provides no per-environment name), so fail fast rather than fall back to
        an ambiguous bare ``model_name`` at the call boundary.
        """
        if v is None:
            return v
        if not v:
            raise ValueError(
                "served_model_names must not be empty when declared; "
                "omit the field entirely for single-environment models"
            )
        for env_id, served_name in v.items():
            if not env_id.strip():
                raise ValueError("served_model_names environment id must be non-blank")
            if not served_name.strip():
                raise ValueError(
                    f"served_model_names[{env_id!r}] served name must be non-blank"
                )
        return v

    @field_validator("pricing_per_1m_input", "pricing_per_1m_output", mode="before")
    @classmethod
    def coerce_decimal(cls, v: object) -> Decimal:
        return Decimal(str(v))


class ModelLlmModelRegistry(BaseModel):
    """Versioned model registry with deterministic hash generation.

    The registry_hash field is computed deterministically from the canonical
    JSON serialization of the models dict. Same content always produces same hash.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: str
    model_registry_version: str
    pricing_manifest_version: str
    observed_at: str
    models: dict[str, ModelLlmModelProfile]

    # Computed at load time — not stored in YAML
    model_registry_hash: str = ""
    pricing_manifest_hash: str = ""

    @model_validator(mode="after")
    def compute_hashes(self) -> Self:
        """Compute deterministic SHA-256 hashes over canonical serializations."""
        # Canonical serialization: sort_keys=True, no whitespace variation
        canonical = json.dumps(
            {k: v.model_dump(mode="json") for k, v in sorted(self.models.items())},
            sort_keys=True,
            default=str,
        )
        registry_hash = hashlib.sha256(canonical.encode()).hexdigest()

        # Pricing manifest hash covers only pricing-relevant fields
        pricing_data = {
            k: {
                "pricing_per_1m_input": str(v.pricing_per_1m_input),
                "pricing_per_1m_output": str(v.pricing_per_1m_output),
                "cost_basis": v.cost_basis,
                "observed_at": v.observed_at,
            }
            for k, v in sorted(self.models.items())
        }
        pricing_hash = hashlib.sha256(
            json.dumps(pricing_data, sort_keys=True).encode()
        ).hexdigest()

        # Use object.__setattr__ since model is frozen
        object.__setattr__(self, "model_registry_hash", registry_hash)
        object.__setattr__(self, "pricing_manifest_hash", pricing_hash)
        return self

    def get_model(self, model_id: str) -> ModelLlmModelProfile:
        """Retrieve a model profile by ID. Raises KeyError if not found."""
        if model_id not in self.models:
            raise KeyError(
                f"Model '{model_id}' not found in registry version {self.model_registry_version}"
            )
        return self.models[model_id]

    def resolve_endpoint(self, model_id: str) -> str:
        """Resolve a model's endpoint URL from the declared env var.

        Raises KeyError if model not in registry.
        Raises RuntimeError if env var is not set (fail-fast, no silent defaults).
        """
        profile = self.get_model(model_id)
        env_var = profile.endpoint_env
        value = os.environ.get(
            env_var
        )  # ONEX_FLAG_EXEMPT: registry resolves declared env var names at runtime; values from Infisical/.env
        if not value:
            raise RuntimeError(
                f"Endpoint env var '{env_var}' for model '{model_id}' is not set. "
                "All endpoint env var names are declared in the contract; "
                "actual values must come from runtime config (Infisical / .env)."
            )
        return value


class ModelLlmModelRegistryLoader:
    """Loads and validates the model registry YAML at startup."""

    def __init__(self, registry_path: Path | None = None) -> None:
        self._path = registry_path or _DEFAULT_REGISTRY_PATH

    def load(self) -> ModelLlmModelRegistry:
        """Load the registry from YAML, validate via Pydantic, compute hashes.

        Raises FileNotFoundError if registry file does not exist.
        Raises ValidationError if registry fails schema validation.
        """
        if not self._path.exists():
            raise FileNotFoundError(
                f"Model registry not found at {self._path}. "
                "Ensure the registry YAML is present before starting the delegation pipeline."
            )

        with self._path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # Parse model profiles from the nested dict
        raw_models = raw.get("models", {})
        parsed_models: dict[str, ModelLlmModelProfile] = {}
        for model_id, profile_data in raw_models.items():
            # Ensure model_id field is set from the YAML key
            if "model_id" not in profile_data:
                profile_data["model_id"] = model_id
            parsed_models[model_id] = ModelLlmModelProfile(**profile_data)

        return ModelLlmModelRegistry(
            schema_version=raw["schema_version"],
            model_registry_version=raw["model_registry_version"],
            pricing_manifest_version=raw["pricing_manifest_version"],
            observed_at=raw["observed_at"],
            models=parsed_models,
        )
