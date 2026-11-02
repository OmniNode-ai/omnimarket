# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The artifact identities a side saw, kept apart because their digests differ."""

from __future__ import annotations

from enum import StrEnum


class EnumSeamArtifactKind(StrEnum):
    SOURCE_DOCUMENT_DIGEST = "source_document_digest"
    OUTPUT_CONTENT_DIGEST = "output_content_digest"
    THEME_DIGEST = "theme_digest"
    POLICY_DIGEST = "policy_digest"
