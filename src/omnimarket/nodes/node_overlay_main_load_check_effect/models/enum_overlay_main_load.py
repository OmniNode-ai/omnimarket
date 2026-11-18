# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Finding codes of the overlay main load check node (OMN-23963)."""

from __future__ import annotations

from enum import StrEnum


class EnumOverlayLoadFindingCode(StrEnum):
    DOCUMENT_INVALID = "document_invalid"
    MODEL_IMPORT_FAILED = "model_import_failed"
    PIN_MALFORMED = "pin_malformed"
    MODEL_ABSENT = "model_absent"
    DOCUMENT_ABSENT = "document_absent"
