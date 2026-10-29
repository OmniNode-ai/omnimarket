# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One drive folder of the folder map."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.kb_routing.model_kb_file_selector import ModelKbFileSelector


class ModelKbDriveFolder(BaseModel):
    """A folder key's folder id, subfolder template, access list and file selectors."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    drive_store: str = Field(min_length=1)
    folder_id: str = Field(min_length=1)
    subfolder_template: str | None = None
    access: tuple[str, ...] = ()
    selectors: tuple[ModelKbFileSelector, ...] = ()


__all__ = ["ModelKbDriveFolder"]
