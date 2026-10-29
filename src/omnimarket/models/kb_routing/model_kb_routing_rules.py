# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed knowledge-store routing rules."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.kb_routing.enum_kb_store_kind import EnumKbStoreKind
from omnimarket.models.kb_routing.model_kb_drive_folder import ModelKbDriveFolder
from omnimarket.models.kb_routing.model_kb_privacy_class import ModelKbPrivacyClass
from omnimarket.models.kb_routing.model_kb_routing_error import KbRoutingRulesError
from omnimarket.models.kb_routing.model_kb_store import ModelKbStore


def _inject_keys(section: Any, key_field: str) -> Any:
    """Fill each mapping entry's identity field from its key; refuse a mismatch."""
    if not isinstance(section, dict):
        return section
    out: dict[str, Any] = {}
    for key, entry in section.items():
        if not isinstance(entry, dict):
            out[key] = entry
            continue
        declared = entry.get(key_field, key)
        if declared != key:
            raise KbRoutingRulesError(
                f"entry keyed {key!r} declares {key_field} {declared!r}"
            )
        out[key] = {**entry, key_field: key}
    return out


class ModelKbRoutingRules(BaseModel):
    """Stores, privacy classes, path templates, threshold and the drive folder map.

    The audience order runs from narrowest to widest and is data. Validation is
    on the loaded rules: an audience must be in the order, a class default store
    must exist and be no wider than the class maximum, a sensitive class store
    must be no wider than the sensitive ceiling, every required class must have
    a rule, every external store must declare checks, every folder must name a
    drive store.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    audience_order: tuple[str, ...] = Field(min_length=1)
    sensitive_audience_ceiling: str = Field(min_length=1)
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    required_classes: tuple[str, ...] = ()
    stores: dict[str, ModelKbStore]
    classes: dict[str, ModelKbPrivacyClass]
    drive_folders: dict[str, ModelKbDriveFolder] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _inject_identities(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {
            **data,
            "stores": _inject_keys(data.get("stores"), "id"),
            "classes": _inject_keys(data.get("classes"), "key"),
            "drive_folders": _inject_keys(data.get("drive_folders"), "key"),
        }

    def audience_rank(self, audience: str) -> int:
        """Position in the audience order; 0 is narrowest."""
        try:
            return self.audience_order.index(audience)
        except ValueError:
            raise KbRoutingRulesError(
                f"audience {audience!r} is not in the audience order"
            ) from None

    @model_validator(mode="after")
    def _validate_rules(self) -> ModelKbRoutingRules:
        if len(set(self.audience_order)) != len(self.audience_order):
            raise KbRoutingRulesError("audience order repeats an audience")
        ceiling = self.audience_rank(self.sensitive_audience_ceiling)
        for store in self.stores.values():
            self.audience_rank(store.audience)
            if store.kind.is_external and not store.required_checks:
                raise KbRoutingRulesError(
                    f"external store {store.id!r} declares no required checks"
                )
        for key in self.required_classes:
            if key not in self.classes:
                raise KbRoutingRulesError(f"class {key!r} has no rule")
        for cls in self.classes.values():
            routed = self.stores.get(cls.default_store)
            if routed is None:
                raise KbRoutingRulesError(
                    f"class {cls.key!r} routes to unknown store {cls.default_store!r}"
                )
            store_rank = self.audience_rank(routed.audience)
            if store_rank > self.audience_rank(cls.max_audience):
                raise KbRoutingRulesError(
                    f"class {cls.key!r} routes to store {routed.id!r} whose audience "
                    f"{routed.audience!r} is wider than the class maximum "
                    f"{cls.max_audience!r}"
                )
            if cls.sensitive and store_rank > ceiling:
                raise KbRoutingRulesError(
                    f"sensitive class {cls.key!r} routes to store {routed.id!r} whose "
                    f"audience {routed.audience!r} is wider than "
                    f"{self.sensitive_audience_ceiling!r}"
                )
        for folder in self.drive_folders.values():
            drive = self.stores.get(folder.drive_store)
            if drive is None or drive.kind != EnumKbStoreKind.DRIVE:
                raise KbRoutingRulesError(
                    f"folder {folder.key!r} names {folder.drive_store!r}, "
                    "which is not a drive store"
                )
        return self


__all__ = ["ModelKbRoutingRules"]
