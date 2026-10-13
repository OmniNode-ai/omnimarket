# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load and validate a deployment's branch-protection audit overlay."""

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_branch_protection_audit_effect.models import (
    OVERLAY_SCHEMA_VERSION,
    ModelBranchProtectionAuditPolicy,
    ModelBranchProtectionPolicyLoadRequest,
)


class BranchProtectionOverlayError(ValueError):
    """An overlay cannot supply a valid audit policy."""


class HandlerBranchProtectionPolicyLoad:
    """Replace default policy lists with the lists explicitly supplied in YAML."""

    def handle(
        self, request: ModelBranchProtectionPolicyLoadRequest
    ) -> ModelBranchProtectionAuditPolicy:
        path = request.overlay_path
        if path is None:
            if request.overlay_required:
                raise BranchProtectionOverlayError("overlay is required")
            return ModelBranchProtectionAuditPolicy()
        if not path.is_file():
            raise BranchProtectionOverlayError(f"overlay not found: {path}")
        try:
            raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise BranchProtectionOverlayError(f"invalid overlay YAML: {exc}") from exc
        except (OSError, UnicodeError) as exc:
            raise BranchProtectionOverlayError(
                f"could not read overlay: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise BranchProtectionOverlayError("overlay must be a mapping")
        if raw.get("schema_version") != OVERLAY_SCHEMA_VERSION:
            raise BranchProtectionOverlayError(
                f"overlay schema_version must be {OVERLAY_SCHEMA_VERSION!r}"
            )
        fields = ModelBranchProtectionAuditPolicy.model_fields
        updates: dict[str, tuple[str, ...]] = {}
        for key, value in raw.items():
            if key == "schema_version":
                continue
            if not isinstance(key, str) or key not in fields:
                raise BranchProtectionOverlayError(f"overlay unknown key: {key!r}")
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise BranchProtectionOverlayError(
                    f"overlay {key} must be a list of strings"
                )
            updates[key] = tuple(value)
        try:
            return ModelBranchProtectionAuditPolicy.model_validate(updates)
        except ValidationError as exc:
            raise BranchProtectionOverlayError(str(exc)) from exc
