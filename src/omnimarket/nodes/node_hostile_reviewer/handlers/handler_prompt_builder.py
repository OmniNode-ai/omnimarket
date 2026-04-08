# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Prompt Builder — pure COMPUTE handler for review workflows.

Takes (prompt_template_id, context_content, model_context_window), returns
(system_prompt, user_prompt). Applies head+tail truncation when content exceeds
the token budget for the target model.

No I/O. Deterministic given the same inputs.

Canonical prompt source: omniintelligence.review_pairing.prompts.adversarial_reviewer
Prompt constants are inlined here to avoid a cross-repo runtime dependency.

Reference: OMN-7794, OMN-7781
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Prompt constants (canonical source: omniintelligence adversarial_reviewer.py)
# Keep in sync with PROMPT_VERSION 1.1.0
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT: str = (
    "You are an adversarial plan reviewer with PhD-level expertise in "
    "software architecture, distributed systems, security, and testing.\n"
    "\n"
    "## Reviewer Profile\n"
    "\n"
    "- Skeptical by design. Generally disagrees with the author's "
    "conclusions and assumptions.\n"
    "- Does not praise. If something is adequate, say nothing about it.\n"
    "- Pithy and analytical. Prioritizes intellectual honesty over "
    "politeness; embraces brevity.\n"
    "- Kind but unsentimental. Does not suffer fools.\n"
    "- Refuses bad faith arguments; cuts down bad faith statements when "
    "necessary.\n"
    "- Wry, subtle wit only; avoids superfluous or flowery speech.\n"
    "- Highlights failures of critical evaluation.\n"
    "- Assists open-ended inquiry and scientific theory creation.\n"
    "\n"
    "## Tone and Style\n"
    "\n"
    "- Journal-style critique format. Default to finding problems.\n"
    "- Never uses em dashes, emdashes, or double hyphens. Use commas, "
    "semicolons, or periods instead.\n"
    "- No editorializing, colloquialisms, or user praise.\n"
    "- No subjective qualifiers, value judgments, enthusiasm, or signaling "
    "of agreement.\n"
    "- Never starts a sentence with 'ah the old'.\n"
    "- Avoids 'it's not just X' constructions.\n"
    "- Avoids language revealing LLM architecture.\n"
    "- All claims cross-referenced against current consensus, with failures "
    "of critical evaluation or lack of consensus explicitly identified.\n"
    "- Unsubstantiated architectural claims evaluated against peer-reviewed "
    "patterns and industry-standard references where applicable.\n"
    "\n"
    "## Output Format\n"
    "\n"
    "Your output MUST be a JSON array of findings. Each finding is an object "
    "with exactly these fields:\n"
    "\n"
    '- "category": string, one of "architecture", "security", "performance", '
    '"correctness", "completeness", "feasibility", "testing", "style"\n'
    '- "severity": string, one of "critical", "major", "minor", "nit"\n'
    '- "title": string, short label (under 80 chars)\n'
    '- "description": string, detailed explanation of the issue\n'
    '- "evidence": string, specific text or section from the plan that '
    "demonstrates the issue\n"
    '- "proposed_fix": string, concrete suggestion for how to address it\n'
    '- "location": string or null, file path or section reference if '
    "applicable\n"
    "\n"
    "Do not include any text outside the JSON array. Do not wrap the array "
    "in markdown fences. Output only the raw JSON array.\n"
    "\n"
    "## Severity Definitions\n"
    "\n"
    "- critical: Security vulnerability, data loss risk, architectural flaw "
    "that would require redesign, or internally inconsistent contract that "
    "breaks substitutability.\n"
    "- major: Performance issue, missing error handling, incomplete test "
    "coverage for critical paths, or API design that will cause integration "
    "pain.\n"
    "- minor: Code quality concern, documentation gap, edge case not "
    "addressed, or suboptimal but functional design choice.\n"
    "- nit: Formatting, naming convention, minor refactoring suggestion, "
    "or stylistic preference with no functional impact.\n"
    "\n"
    "## General Principle: Rigorous Objectivity\n"
    "\n"
    "Responses prioritize concise, factual, and analytical content. "
    "All output is devoid of subjective qualifiers, value judgments, "
    "enthusiasm, or signaling of agreement. Treat every request as "
    "serious, time-sensitive, and precision-critical."
)

_USER_PROMPT_TEMPLATE: str = (
    "Review the following technical plan. Apply rigorous objectivity. "
    "Identify all weaknesses, unstated assumptions, missing error handling, "
    "architectural risks, and feasibility concerns. Cut through any "
    "vagueness or hand-waving in the plan.\n"
    "\n"
    "Return your findings as a JSON array following the specified schema.\n"
    "\n"
    "---\n"
    "\n"
    "{plan_content}"
)

_USER_PROMPT_TEMPLATE_PR: str = (
    "Review the following pull request diff. Apply rigorous objectivity. "
    "Identify security vulnerabilities, logic errors, missing error handling, "
    "race conditions, performance regressions, API contract violations, "
    "untested edge cases, and architectural concerns.\n"
    "\n"
    "Focus on what the diff actually changes. Do not flag pre-existing issues "
    "in unchanged code. Every finding must reference a specific change in "
    "the diff.\n"
    "\n"
    "Return your findings as a JSON array following the specified schema.\n"
    "\n"
    "---\n"
    "\n"
    "{plan_content}"
)

# Rough chars-per-token estimate for budget calculation.
# Conservative (3.5 chars/token) to avoid overrunning context windows.
_CHARS_PER_TOKEN = 3.5
_RESPONSE_RESERVE_TOKENS = 2048


class ModelPromptBuilderInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_template_id: str = Field(
        ..., description="One of: adversarial_reviewer_pr, adversarial_reviewer_plan"
    )
    context_content: str = Field(..., description="The diff or plan content.")
    model_context_window: int = Field(
        ..., ge=1024, description="Target model context window in tokens."
    )
    persona_markdown: str | None = Field(
        default=None,
        description="Optional persona tone directive prepended to system prompt.",
    )


class ModelPromptBuilderOutput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    system_prompt: str = Field(...)
    user_prompt: str = Field(...)
    truncated: bool = Field(default=False)
    original_content_chars: int = Field(default=0)
    truncated_content_chars: int = Field(default=0)


_TEMPLATES: dict[str, str] = {
    "adversarial_reviewer_pr": _USER_PROMPT_TEMPLATE_PR,
    "adversarial_reviewer_plan": _USER_PROMPT_TEMPLATE,
}


def _truncate_head_tail(content: str, max_chars: int) -> str:
    """Head+tail truncation: keep first 60% and last 40%."""
    if len(content) <= max_chars:
        return content
    head_size = int(max_chars * 0.6)
    tail_size = max_chars - head_size
    return (
        content[:head_size]
        + "\n\n... [truncated: middle section removed to fit model context window] ...\n\n"
        + content[-tail_size:]
    )


def build_prompt(input_data: ModelPromptBuilderInput) -> ModelPromptBuilderOutput:
    """Build (system_prompt, user_prompt) from template + context + window size."""
    template = _TEMPLATES.get(input_data.prompt_template_id)
    if template is None:
        msg = f"Unknown prompt_template_id: {input_data.prompt_template_id!r}"
        raise ValueError(msg)

    system = _SYSTEM_PROMPT
    if input_data.persona_markdown:
        system = input_data.persona_markdown.rstrip() + "\n\n" + system

    # Budget: context_window - system_prompt_tokens - response_reserve
    system_tokens = int(len(system) / _CHARS_PER_TOKEN)
    content_budget_tokens = (
        input_data.model_context_window - system_tokens - _RESPONSE_RESERVE_TOKENS
    )
    content_budget_chars = int(content_budget_tokens * _CHARS_PER_TOKEN)

    original_chars = len(input_data.context_content)
    truncated = original_chars > content_budget_chars
    content = (
        _truncate_head_tail(input_data.context_content, content_budget_chars)
        if truncated
        else input_data.context_content
    )

    user_prompt = template.format(plan_content=content)

    return ModelPromptBuilderOutput(
        system_prompt=system,
        user_prompt=user_prompt,
        truncated=truncated,
        original_content_chars=original_chars,
        truncated_content_chars=len(content),
    )


class HandlerPromptBuilder:
    """RuntimeLocal handler protocol wrapper for prompt builder."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to build_prompt with a ModelPromptBuilderInput.
        """
        parsed = ModelPromptBuilderInput(**input_data)
        result = build_prompt(parsed)
        return result.model_dump(mode="json")


__all__: list[str] = [
    "HandlerPromptBuilder",
    "ModelPromptBuilderInput",
    "ModelPromptBuilderOutput",
    "build_prompt",
]
