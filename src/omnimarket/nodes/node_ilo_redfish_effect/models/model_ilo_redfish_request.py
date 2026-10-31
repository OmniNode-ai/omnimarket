# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One request to the iLO 4 Redfish effect (OMN-21835)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    WRITE_VERBS,
    EnumIloBootTarget,
    EnumIloLogKind,
    EnumIloMediaAction,
    EnumIloPowerAction,
    EnumIloVerb,
)


class ModelIloRedfishRequest(BaseModel):
    """A verb, a host, and the parameters that verb takes.

    A write verb carries ``approval_ref``: the timestamp of the OPERATOR-CONSENT ledger row that names
    this action and this host. ``requested_by`` is the lane asking, so a lane cannot approve itself.
    Parameters that do not belong to the verb are rejected, not ignored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: EnumIloVerb
    host: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    requested_by: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
    approval_ref: (
        Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")] | None
    ) = None
    log_kind: EnumIloLogKind | None = None
    log_limit: Annotated[int, Field(ge=1, le=500)] = 50
    power_action: EnumIloPowerAction | None = None
    media_action: EnumIloMediaAction | None = None
    image_url: (
        Annotated[str, Field(min_length=8, max_length=1024, pattern=r"^\S+$")] | None
    ) = None
    boot_target: EnumIloBootTarget | None = None

    @model_validator(mode="after")
    def _parameters_match_the_verb(self) -> ModelIloRedfishRequest:
        owned: dict[EnumIloVerb, set[str]] = {
            EnumIloVerb.LOGS: {"log_kind"},
            EnumIloVerb.POWER: {"power_action"},
            EnumIloVerb.VIRTUAL_MEDIA: {"media_action", "image_url"},
            EnumIloVerb.BOOT_ONCE: {"boot_target"},
        }
        given = {
            name
            for name in (
                "log_kind",
                "power_action",
                "media_action",
                "image_url",
                "boot_target",
            )
            if getattr(self, name) is not None
        }
        allowed = owned.get(self.verb, set())
        if given - allowed:
            raise ValueError(
                f"{self.verb.value} does not take {sorted(given - allowed)}"
            )
        required = {
            EnumIloVerb.LOGS: {"log_kind"},
            EnumIloVerb.POWER: {"power_action"},
            EnumIloVerb.VIRTUAL_MEDIA: {"media_action"},
            EnumIloVerb.BOOT_ONCE: {"boot_target"},
        }.get(self.verb, set())
        if required - given:
            raise ValueError(f"{self.verb.value} requires {sorted(required - given)}")
        if self.verb is EnumIloVerb.VIRTUAL_MEDIA:
            inserting = self.media_action is EnumIloMediaAction.INSERT
            if inserting != (self.image_url is not None):
                raise ValueError(
                    "image_url is required for insert and not allowed for eject"
                )
        if self.verb not in WRITE_VERBS and self.approval_ref is not None:
            raise ValueError("a read verb takes no approval_ref")
        return self

    @property
    def is_write(self) -> bool:
        return self.verb in WRITE_VERBS

    @property
    def action_label(self) -> str:
        """The ``action=`` token an approval row must carry for this request."""
        if self.verb is EnumIloVerb.POWER and self.power_action is not None:
            return f"power:{self.power_action.value}"
        if self.verb is EnumIloVerb.VIRTUAL_MEDIA and self.media_action is not None:
            return f"virtual_media:{self.media_action.value}"
        if self.verb is EnumIloVerb.BOOT_ONCE and self.boot_target is not None:
            return f"boot_once:{self.boot_target.value}"
        return self.verb.value
