# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded string types shared by the compaction receipts (OMN-21112).

A receipt may hold a string only as an id, a hash, an enum member or a name, so
each string type here is fenced by a pattern that the JSON schema carries.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

SHA256_PATTERN = r"^[0-9a-f]{64}$"
SESSION_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$"

Sha256Hex = Annotated[str, Field(pattern=SHA256_PATTERN)]
SessionId = Annotated[str, Field(pattern=SESSION_ID_PATTERN)]
TranscriptRecordId = Annotated[str, Field(pattern=SESSION_ID_PATTERN)]
Name = Annotated[str, Field(pattern=NAME_PATTERN)]

__all__ = [
    "NAME_PATTERN",
    "SESSION_ID_PATTERN",
    "SHA256_PATTERN",
    "Name",
    "SessionId",
    "Sha256Hex",
    "TranscriptRecordId",
]
