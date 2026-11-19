# SPDX-License-Identifier: Apache-2.0
"""Ownership stamp and health-check models the deploy receipt reader keeps.

The deploy agent's terminal events carry an ownership stamp -- ``instance`` and
``host``, set only by an instance with the ``commands_only`` flag -- plus
``lane_mirror`` and ``sibling_refs`` evidence. The reader used to drop all of them
under ``extra="ignore"``, so a receipt could not say who ran the rebuild or whether
the departure can be accepted as ours (:attr:`ModelDeployStamp.accepted_as_ours`).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)


class ModelHealthCheck(BaseModel):
    """Single service health check result."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    service: str = Field(..., description="Service name.")
    endpoint: str = Field(..., description="Endpoint checked.")
    status: Literal["pass", "fail"] = Field(..., description="Check result.")
    latency_ms: int = Field(default=0, description="Latency in milliseconds.")


class ModelDeployStamp(BaseModel):
    """Ownership stamp and evidence mappings carried on a terminal deploy event."""

    model_config = ConfigDict(frozen=True, extra="ignore", from_attributes=True)

    instance: str | None = Field(
        default=None,
        description=(
            "Deploy-agent instance that handled the command, set only by an "
            "instance with the commands_only flag; absent from the wire otherwise."
        ),
    )
    host: str | None = Field(
        default=None,
        description=(
            "Host the command was handled on, set only by a commands_only "
            "instance; absent from the wire otherwise."
        ),
    )
    lane_mirror: dict[str, object] | None = Field(
        default=None,
        description="Lane-mirror evidence a completed event carries; null if none.",
    )
    sibling_refs: dict[str, str] | None = Field(
        default=None,
        description="Per-repository sibling evidence (repo -> ref) for a rebuild.",
    )

    @property
    def accepted_as_ours(self) -> bool:
        """Whether this receipt can be accepted as OUR departure.

        True only when ``instance`` and ``host`` are both non-empty AND the event is
        a successful completion. A failed rebuild that names its instance is kept
        but never accepted, and a rejection (no ``status``) never ran, so it is
        never accepted either.
        """
        return (
            bool(self.instance)
            and bool(self.host)
            and getattr(self, "status", None) == "success"
        )

    @model_serializer(mode="wrap")
    def _dump_with_acceptance(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Carry ``accepted_as_ours`` into every dump, as a derived field."""
        dumped: dict[str, Any] = handler(self)
        dumped["accepted_as_ours"] = self.accepted_as_ours
        return dumped
