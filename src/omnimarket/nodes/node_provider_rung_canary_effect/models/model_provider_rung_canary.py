# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, per-rung verdict and result models of the provider-rung canary (OMN-16987).

Nothing here carries key material: a verdict names the secret REFERENCE (the key
name the contract declares), never a resolved value.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumProviderRungVerdict(StrEnum):
    """The typed outcome of probing one declared backend."""

    LIVE = "LIVE"
    AUTH_DEAD = "AUTH_DEAD"
    QUOTA_DEAD = "QUOTA_DEAD"
    UNREACHABLE = "UNREACHABLE"
    UNRESOLVED = "UNRESOLVED"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    HTTP_ERROR = "HTTP_ERROR"
    PROBE_ERROR = "PROBE_ERROR"
    SKIPPED_NO_ENDPOINT = "SKIPPED_NO_ENDPOINT"
    SKIPPED_NO_SECRET_REF = "SKIPPED_NO_SECRET_REF"
    SKIPPED_SUPPRESSED = "SKIPPED_SUPPRESSED"


class ModelProviderRungSuppression(BaseModel):
    """A backend that must not be re-probed before ``until`` (a provider-stated reset)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(..., min_length=1)
    until: datetime = Field(..., description="Instant the backend may be probed again.")
    quota_code: str | None = Field(default=None)


class ModelProviderRungCanaryRequest(BaseModel):
    """Which contract to probe and the suppression state carried from the last run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_path: str | None = Field(
        default=None,
        description="Bifrost delegation contract; None resolves it as the runtime does.",
    )
    overlay_path: str | None = Field(default=None)
    state_path: str | None = Field(
        default=None,
        description="JSON file holding suppressions between runs; read first, rewritten last.",
    )
    suppressions: tuple[ModelProviderRungSuppression, ...] = Field(default=())
    wrong_key_control: bool = Field(
        default=True,
        description="Send one deliberately invalid key per probed endpoint; it must read AUTH_DEAD.",
    )
    now: datetime | None = Field(default=None, description="Clock override for tests.")


class ModelProviderRungVerdict(BaseModel):
    """One declared backend, one verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str
    provider: str | None = None
    tier: str | None = None
    model_name: str | None = None
    secret_ref: str | None = Field(
        default=None, description="The key NAME, never its value."
    )
    verdict: EnumProviderRungVerdict
    probed: bool = Field(
        default=False,
        description="True for a credentialed backend that was not skipped (UNRESOLVED counts).",
    )
    request_sent: bool = Field(
        default=False, description="True when an HTTP request left for it."
    )
    endpoint_host: str | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    provider_error_code: str | None = None
    provider_error_message: str | None = None
    exception: str | None = None
    quota_class: str | None = Field(
        default=None,
        description="The contract's disposition for the quota code (z.ai 1310 vs 1113).",
    )
    suppressed_until: datetime | None = None
    shared_probe_with: tuple[str, ...] = ()


class ModelProviderRungControl(BaseModel):
    """The wrong-key control for one endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_host: str | None
    verdict: EnumProviderRungVerdict
    http_status: int | None = None

    @property
    def bites(self) -> bool:
        return self.verdict is EnumProviderRungVerdict.AUTH_DEAD


class ModelProviderRungCanaryResult(BaseModel):
    """Every declared backend with its verdict, the controls and the state to carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    rungs: tuple[ModelProviderRungVerdict, ...]
    controls: tuple[ModelProviderRungControl, ...] = ()
    suppressions: tuple[ModelProviderRungSuppression, ...] = ()
    key_echoes: int = Field(
        default=0, description="Occurrences of a resolved key a provider quoted back."
    )
    summary_table: str = ""

    @property
    def probed_count(self) -> int:
        return sum(1 for r in self.rungs if r.probed)

    @property
    def all_live(self) -> bool:
        probed = [r for r in self.rungs if r.probed]
        return (
            bool(probed)
            and all(r.verdict is EnumProviderRungVerdict.LIVE for r in probed)
            and all(c.bites for c in self.controls)
            and self.key_echoes == 0
        )


__all__ = [
    "EnumProviderRungVerdict",
    "ModelProviderRungCanaryRequest",
    "ModelProviderRungCanaryResult",
    "ModelProviderRungControl",
    "ModelProviderRungSuppression",
    "ModelProviderRungVerdict",
]
