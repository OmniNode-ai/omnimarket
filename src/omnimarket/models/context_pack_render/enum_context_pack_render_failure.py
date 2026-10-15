# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed render failures (OMN-21109, plan task TA.7)."""

from enum import StrEnum


class EnumContextPackRenderFailure(StrEnum):
    """Why a render refused to produce messages. A refusal never renders a partial pack."""

    MISSING_SOURCE_IDENTIFIER = "missing_source_identifier"
    MISSING_RULING_REFERENCE = "missing_ruling_reference"
    MODEL_ROUTE_IDENTITY_MISSING = "model_route_identity_missing"
    UNCLASSIFIED_FACTOR = "unclassified_factor"
    UNKNOWN_CHUNK_REF = "unknown_chunk_ref"
