"""ModelRedeployCommand — command to start post-release redeploy."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EnumRuntimeLane(StrEnum):
    """Runtime lane targeted by a deployment request or proof (OMN-12576).

    Values match the OCC-owned wire schema enum and the live ``.201`` runtime
    lanes: dev (8085/8086, omnibase-infra), stability-test (18085/18086,
    omnibase-infra-stability-test), prod (28085/28086, omnibase-infra-prod).
    """

    DEV = "dev"
    STABILITY_TEST = "stability-test"
    PROD = "prod"


class ModelRedeployCommand(BaseModel):
    """Command to start a post-release runtime redeploy.

    OMN-12576 evolves this command in place (rather than forking a parallel
    deployment-request contract) with the lane/digest/promotion fields the
    node-based deployment design needs. The new fields are optional so existing
    callers and dev-lane triggers stay valid; production pins ``image_digest``
    and ``promotion_batch_id`` to the artifact proven READY in stability-test.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Redeploy run correlation ID.")
    versions: dict[str, str] = Field(
        default_factory=dict,
        description="Plugin version pins (pkg -> version).",
    )
    skip_sync: bool = Field(default=False, description="Skip SYNC_CLONES phase.")
    verify_only: bool = Field(default=False, description="Skip to VERIFY_HEALTH only.")
    dry_run: bool = Field(default=False, description="Print without executing.")
    requested_at: datetime = Field(..., description="When the command was issued.")
    runtime_lane: EnumRuntimeLane = Field(
        default=EnumRuntimeLane.DEV,
        description="Target runtime lane for this deployment.",
    )
    image_ref: str | None = Field(
        default=None,
        description="Mutable image reference. The digest is the authority, not the ref.",
    )
    image_digest: str | None = Field(
        default=None,
        description=(
            "Immutable image digest. Required for prod; pinned to the "
            "stability-test READY digest."
        ),
    )
    promotion_batch_id: str | None = Field(
        default=None,
        description="Promotion batch identifier shared with OCC evidence; required for prod.",
    )

    @model_validator(mode="after")
    def validate_prod_pins(self) -> ModelRedeployCommand:
        """Production redeploys must pin the proven stability artifact."""
        if self.runtime_lane is EnumRuntimeLane.PROD and (
            not self.image_digest or not self.promotion_batch_id
        ):
            raise ValueError(
                "prod redeploy requires image_digest and promotion_batch_id"
            )
        return self


__all__: list[str] = ["EnumRuntimeLane", "ModelRedeployCommand"]
