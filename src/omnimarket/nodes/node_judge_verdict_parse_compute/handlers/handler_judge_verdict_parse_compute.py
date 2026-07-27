# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_judge_verdict_parse_compute (OMN-13212 / B2).

COMPUTE node. Pure transformation: parses one judge model's raw PASS/FAIL JSON
response into a typed ``ModelJudgeParseResult``. Re-expresses the pure parse
logic extracted from the deleted ``node_pr_review_bot.handler_judge_verifier``
``_parse_judge_response`` — the raw-httpx judge call is deleted (the orchestrator
now calls the judge via the canonical inference bridge). No I/O.

Fail-closed: malformed JSON or an unknown verdict yields ``passed=False`` with a
clear reasoning message; ``passed`` is the only authority field.
"""

from __future__ import annotations

import json
import logging

from omnimarket.review.pr_review_node_io import (
    ModelJudgeParseRequest,
    ModelJudgeParseResult,
)

_log = logging.getLogger(__name__)


def parse_judge_response(raw: str) -> ModelJudgeParseResult:
    """Parse the judge model JSON response into a typed verdict.

    Returns ``passed=True`` only for an explicit ``PASS`` verdict. Markdown code
    fences are stripped. On parse failure or an unknown verdict, returns
    ``passed=False`` with a clear reasoning message (fail-closed).
    """
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(
                lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:]
            )
        data = json.loads(cleaned)
        verdict = str(data.get("verdict", "")).upper()
        reasoning = str(data.get("reasoning", "No reasoning provided."))
        if verdict not in {"PASS", "FAIL"}:
            return ModelJudgeParseResult(
                passed=False,
                reasoning=f"Judge returned unknown verdict {verdict!r}. Treating as FAIL.",
            )
        return ModelJudgeParseResult(passed=verdict == "PASS", reasoning=reasoning)
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        _log.warning("Judge response parse failure: %s | raw=%r", exc, raw[:200])
        return ModelJudgeParseResult(
            passed=False,
            reasoning=(
                f"Judge model returned malformed JSON (parse error: {exc}). "
                "Treating as FAIL."
            ),
        )


class HandlerJudgeVerdictParseCompute:
    """COMPUTE: parse a judge model's raw PASS/FAIL response into a typed verdict."""

    def handle(self, request: ModelJudgeParseRequest) -> ModelJudgeParseResult:
        """Parse the raw judge response. Pure; returns the typed result directly."""
        return parse_judge_response(request.raw_text)


__all__: list[str] = ["HandlerJudgeVerdictParseCompute", "parse_judge_response"]
