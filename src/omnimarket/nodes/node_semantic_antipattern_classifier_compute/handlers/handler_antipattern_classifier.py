# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure deterministic antipattern violation classifier.

COMPUTE node — no network, no file I/O, no randomness.
Same input always produces same output.

Classification rules (evaluated per match):
1. file_path with line_count < 10 → skip (empty file exemption)
2. similarity < threshold → skip (below detection threshold)
3. similarity >= threshold AND enforcement == "blocking" → blocking violation
4. similarity >= threshold AND enforcement != "blocking" → advisory violation
"""

from __future__ import annotations

from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_request import (
    ModelAntipatternClassifyRequest,
    ModelAntipatternMatch,
)
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_result import (
    ModelAntipatternClassifyResult,
    ModelAntipatternViolation,
)

_MIN_FILE_LINES = 10


def _build_explanation(match: ModelAntipatternMatch, is_blocking: bool) -> str:
    severity = "blocking" if is_blocking else "advisory"
    return (
        f"{match.label} detected in {match.file_path} "
        f"(similarity={match.similarity:.2f}, enforcement={severity}): "
        f"{match.description}"
    )


class HandlerAntipatternClassifier:
    """Pure deterministic classifier. No I/O. Idempotent."""

    def handle(
        self, request: ModelAntipatternClassifyRequest
    ) -> ModelAntipatternClassifyResult:
        threshold = request.config.similarity_threshold
        violations: list[ModelAntipatternViolation] = []

        for match in request.matches:
            if match.line_count < _MIN_FILE_LINES:
                continue
            if match.similarity < threshold:
                continue
            is_blocking = match.enforcement == "blocking"
            violations.append(
                ModelAntipatternViolation(
                    pattern_id=match.pattern_id,
                    label=match.label,
                    file_path=match.file_path,
                    similarity=match.similarity,
                    is_blocking=is_blocking,
                    explanation=_build_explanation(match, is_blocking),
                )
            )

        return ModelAntipatternClassifyResult(violations=tuple(violations))


__all__ = ["HandlerAntipatternClassifier"]
