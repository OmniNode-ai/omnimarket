"""ModelDodVerifyStartCommand — command to start DoD verification."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class ModelDodVerifyStartCommand(BaseModel):
    """Command to start DoD evidence verification.

    ``ticket_id`` is the only caller-supplied field required for routing.
    ``correlation_id`` and ``requested_at`` default when absent so the typed
    command validates against a bare ``onex run-node`` payload such as
    ``{"ticket_id": "OMN-1234", "contract_path": null}`` (OMN-12420). The
    runtime supplies its own envelope correlation_id; these defaults only cover
    the in-process / direct-dispatch paths where the caller omits them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="Linear ticket ID (e.g. OMN-1234).")
    correlation_id: UUID = Field(
        default_factory=uuid4, description="Verification run correlation ID."
    )
    contract_path: str | None = Field(
        default=None, description="Override path to contract YAML."
    )
    dry_run: bool = Field(default=False)
    requested_at: datetime = Field(
        default_factory=_utc_now, description="When the command was issued."
    )


__all__: list[str] = ["ModelDodVerifyStartCommand"]
