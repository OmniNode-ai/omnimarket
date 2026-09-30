# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider surfaces: one host, two separately metered products (OMN-20154).

See ``configs/provider_surfaces.v1.yaml``. A surface names the quota rule that
covers an endpoint under its path prefix and the only models a backend there may
carry. The declarations are read here, for the quota policy (which rule covers an
endpoint) and for the bifrost loader (which models a surface admits).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_SCHEMA_VERSION = "provider_surfaces.v1"


class ModelProviderSurface(BaseModel):
    """One separately metered surface on a provider host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=1)
    match_endpoint_host: str = Field(min_length=1)
    match_path_prefix: str = Field(pattern=r"^/")
    allowed_model_names: tuple[str, ...] = Field(min_length=1)
    allowed_model_names_hint: str | None = None

    def as_rule(self) -> dict[str, Any]:
        """The surface as the mapping shape the loader's rejector reads."""
        return self.model_dump()


def _surfaces_path() -> Path:
    return Path(__file__).resolve().parents[1] / "configs" / "provider_surfaces.v1.yaml"


@lru_cache(maxsize=1)
def load_provider_surfaces() -> tuple[ModelProviderSurface, ...]:
    """Load the committed surfaces. Fails loud: a malformed file is a config error."""
    path = _surfaces_path()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"{path} is not a {_SCHEMA_VERSION} document")
    entries = raw.get("surfaces")
    if not isinstance(entries, list):
        raise ValueError(f"{path} declares no 'surfaces' list")
    return tuple(ModelProviderSurface.model_validate(e) for e in entries)


def provider_surface_for_endpoint(endpoint_url: str) -> ModelProviderSurface | None:
    """Return the surface whose host and longest path prefix match, or ``None``."""
    from urllib.parse import urlparse

    parsed = urlparse(endpoint_url)
    host = (parsed.hostname or "").lower()
    best: ModelProviderSurface | None = None
    for surface in load_provider_surfaces():
        match = surface.match_endpoint_host.lower()
        if not (host == match or host.endswith(f".{match}")):
            continue
        if not parsed.path.startswith(surface.match_path_prefix):
            continue
        if best is None or len(surface.match_path_prefix) > len(best.match_path_prefix):
            best = surface
    return best


__all__: list[str] = [
    "ModelProviderSurface",
    "load_provider_surfaces",
    "provider_surface_for_endpoint",
]
