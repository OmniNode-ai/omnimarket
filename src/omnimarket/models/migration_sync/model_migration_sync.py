# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models the migration-sync nodes hand each other (OMN-20687).

The effect node reads an inventory of the omnimarket source tree, the vendored tree
and the application-migrations manifest; the compute node decides from it; the effect
node applies the actions it decided. These types are the JSON between them, so both
nodes import them from here and neither imports the other.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")
SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ModelMigrationSyncFile(BaseModel):
    """One ``*.sql`` file read from a tree.

    ``relative_path`` is ``<node>/<file>.sql`` for a source file (the node is the
    directory two levels above the file, as the old script derived it) and the path
    below the vendored root for a vendored file. ``source_path`` is the file's
    absolute path on the host that read it; vendored files carry none.
    """

    model_config = _FROZEN

    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    source_path: str = ""


class ModelMigrationSyncManifestRow(BaseModel):
    """One application-migrations.tsv row: its first column and its sixth.

    ``declared_sha256`` is the raw sixth column; a short row leaves it empty and a
    row the old script would have read with a trailing newline in it is kept as read.
    """

    model_config = _FROZEN

    artifact_path: str
    declared_sha256: str = ""


class ModelMigrationSyncLine(BaseModel):
    """One line the old script printed: its text and which stream it went to."""

    model_config = _FROZEN

    stream: Literal["out", "err"]
    text: str


class ModelMigrationSyncAction(BaseModel):
    """One change to the vendored tree, addressed below the vendored root."""

    model_config = _FROZEN

    kind: Literal["copy", "remove"]
    relative_path: str = Field(min_length=1)
    source_path: str = ""

    @model_validator(mode="after")
    def _confined_and_complete(self) -> ModelMigrationSyncAction:
        parts = self.relative_path.split("/")
        if self.relative_path.startswith("/") or any(p in ("", "..") for p in parts):
            raise ValueError(
                f"relative_path must stay below the vendored root: {self.relative_path!r}"
            )
        if self.kind == "copy" and not self.source_path:
            raise ValueError("a copy action needs a source_path")
        return self


class ModelMigrationSyncInventory(BaseModel):
    """What the effect node read, in the order the old script walked it.

    ``source_files`` are in the old script's ``find | sort`` order (byte order of the
    absolute path). ``resolved`` is false when no omnimarket source tree could be
    found. ``failure`` names a read that failed; an inventory that failed is not
    decided from.
    """

    model_config = _FROZEN

    resolved: bool
    nodes_dir: str = ""
    dest_root: str
    source_files: tuple[ModelMigrationSyncFile, ...] = ()
    vendored_files: tuple[ModelMigrationSyncFile, ...] = ()
    manifest_present: bool = False
    manifest_rows: tuple[ModelMigrationSyncManifestRow, ...] = ()
    failure: str = ""
