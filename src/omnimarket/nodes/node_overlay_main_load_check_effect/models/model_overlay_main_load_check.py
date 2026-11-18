# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the overlay main load check node (OMN-23963): the command, findings, and result."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_overlay_main_load_check_effect.models.enum_overlay_main_load import (
    EnumOverlayLoadFindingCode,
)


class ModelOverlayMainLoadCommand(BaseModel):
    """The overlays root and pins file to check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlays_root: str = Field(min_length=1)
    pins_file: str = Field(min_length=1)


class ModelOverlayLoadFinding(BaseModel):
    """One finding of the overlay main load check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    code: EnumOverlayLoadFindingCode
    detail: str

    @property
    def refuses(self) -> bool:
        return self.code in (
            EnumOverlayLoadFindingCode.DOCUMENT_INVALID,
            EnumOverlayLoadFindingCode.MODEL_IMPORT_FAILED,
            EnumOverlayLoadFindingCode.PIN_MALFORMED,
        )


class ModelOverlayMainLoadResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    loaded: tuple[str, ...]
    findings: tuple[ModelOverlayLoadFinding, ...]
