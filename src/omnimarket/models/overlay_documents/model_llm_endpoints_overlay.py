# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the overlay home validates overlays/llm_endpoints/overlay.yaml through (OMN-24024).

omnibase_infra reads the same document through its own ModelLlmEndpointOverlay (the deploy guard
resolves each endpoint slot marked endpoint_from_overlay from it); no value lives here.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelLlmEndpointsOverlay(BaseModel):
    """The LLM endpoints overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    endpoint_urls: dict[str, str] = Field(min_length=1)

    @field_validator("endpoint_urls")
    @classmethod
    def _validate_endpoint_urls(cls, urls: dict[str, str]) -> dict[str, str]:
        """Refuse a malformed slot or URL; store each URL as scheme://host:port with no trailing slash."""
        normalized: dict[str, str] = {}
        for slot_id, url in urls.items():
            if not slot_id or any(c.isspace() for c in slot_id):
                raise ValueError(
                    f"slot id {slot_id!r} must be a non-empty string without whitespace"
                )
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(f"endpoint {slot_id!r} must use http or https")
            if not parsed.hostname:
                raise ValueError(f"endpoint {slot_id!r} must have a hostname")
            if parsed.port is None:
                raise ValueError(f"endpoint {slot_id!r} must have an explicit port")
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError(
                    f"endpoint {slot_id!r} must be a base URL with no path, query or fragment"
                )
            normalized[slot_id] = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        return normalized


__all__ = [
    "ModelLlmEndpointsOverlay",
]
