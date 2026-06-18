# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_hostile_reviewer response parser — re-export of the canonical owner.

The ``parse_model_response`` COMPUTE primitive + its models were re-homed to
``omnimarket.review.response_parser`` in OMN-13208 (A1). This module re-exports
them and keeps the node-internal ``HandlerResponseParser`` RuntimeLocal shim
until the B1 rebuild (OMN-13210) deletes the node.
"""

from __future__ import annotations

from omnimarket.review.response_parser import (
    EnumParseStatus,
    ModelParseResult,
    parse_model_response,
)


class HandlerResponseParser:
    """RuntimeLocal handler protocol wrapper for response parser."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to parse_model_response. Expects input_data with
        'raw_text' and 'source_model' keys.
        """
        raw_text = input_data.get("raw_text")
        if not isinstance(raw_text, str):
            raise TypeError("handle() requires a str in input_data['raw_text']")
        source_model = input_data.get("source_model")
        if not isinstance(source_model, str):
            raise TypeError("handle() requires a str in input_data['source_model']")
        result = parse_model_response(raw_text, source_model)
        return result.model_dump(mode="json")


__all__: list[str] = [
    "EnumParseStatus",
    "HandlerResponseParser",
    "ModelParseResult",
    "parse_model_response",
]
