# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State the rules an answer is refused on in the request it answers.

OMN-18349. The quality gate vetoes an answer on its task class's blocking
rules, and the model answering was told none of them. Measured on the lab,
2026-09-23 (D11 nightly dispatched against the .201 dev lane, run
35920604853, then replayed against the same vLLM endpoint serving
Qwen3.8-27B with thinking off):

* research: 0 of 8 local answers passed ``cites_sources``. The class's own
  system prompt still asked for code-line citations, which OMN-13354 took
  away from research. Stated as a requirement in the user turn: 8 of 8.
* code_review: 0 of 4 passed ``cites_specific_lines``. Stated: 8 of 8.
* document: the ``### ANSWER`` extraction marker, requested only in the
  system prompt, was emitted on 2 of 8 answers, so the extractor blanked the
  other six and the gate scored an empty response. Restated as the first line
  of the user turn: 8 of 8 (see the composer below for why first, not last).

This module is pure. The directive text is read from the one contract the
gate reads (``quality_rules`` in ``task_class_contracts.v1.yaml``), so what the
model is told and what the gate enforces cannot be two values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from omnimarket.inference.task_class_authority import (
    EnumQualityRuleEnforcement,
    resolve_quality_rule,
)

__all__ = [
    "ACCEPTANCE_DIRECTIVES_HEADER",
    "acceptance_rule_names",
    "compose_user_prompt_with_output_directives",
    "render_acceptance_directives",
]

ACCEPTANCE_DIRECTIVES_HEADER = (
    "The answer is accepted only if it meets every one of these requirements:"
)

_REPLACE_TASK_CLASS = "replace_task_class"


def _first_occurrence_only(names: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return tuple(ordered)


def acceptance_rule_names(
    *,
    dod_deterministic: Sequence[str],
    dod_heuristic: Sequence[str],
    acceptance_criteria: Sequence[str],
    quality_contract_mode: str,
) -> tuple[str, ...]:
    """Every rule name the gate will evaluate for this request, in its order.

    Mirrors the gate's selection in ``handler_quality_gate.delta``: under
    ``replace_task_class`` only the caller's criteria are graded, otherwise the
    class's declared rules followed by the caller's criteria. The gate decides
    WHICH BAND each rule lands in; that does not change which rules exist, and
    band placement is not what the model needs to know.
    """
    if quality_contract_mode == _REPLACE_TASK_CLASS:
        return _first_occurrence_only(acceptance_criteria)
    return _first_occurrence_only(
        (*dod_deterministic, *dod_heuristic, *acceptance_criteria)
    )


def render_acceptance_directives(rule_names: Iterable[str]) -> str | None:
    """Render the declared directive of every blocking rule named, or ``None``.

    Only a BLOCKING rule is stated as a requirement: a scored rule is a
    gradient and telling the model it is a veto would be false. A rule with no
    declared ``model_directive`` is not rendered, and neither is a name the
    contract does not declare.
    """
    directives: list[str] = []
    for name in _first_occurrence_only(rule_names):
        rule = resolve_quality_rule(name)
        if rule is None or rule.enforcement is not EnumQualityRuleEnforcement.BLOCKING:
            continue
        if rule.model_directive and rule.model_directive not in directives:
            directives.append(rule.model_directive)
    if not directives:
        return None
    return "\n".join(
        [ACCEPTANCE_DIRECTIVES_HEADER, *(f"- {text}" for text in directives)]
    )


def compose_user_prompt_with_output_directives(
    *,
    prompt: str,
    acceptance_directives: str | None,
    text_shape_instruction: str | None,
) -> str:
    """Compose one user turn: extraction marker, prompt, acceptance rules.

    The extraction-marker sentence (the exact line the extractor locates a text
    deliverable by) goes FIRST, the caller's prompt second and unchanged, the
    acceptance requirements last. Placement was measured on the lab endpoint
    (Qwen3.8-27B, thinking off, 8 runs each), not chosen by taste:

    * marker only in the system prompt: document 2 of 8 carried the marker;
    * marker restated AFTER the prompt: document 8 of 8, but a vague
      code-generation request then came back as a Markdown bullet list, and
      only 4 of 8 compiled (8 of 8 without the restatement);
    * marker restated BEFORE the prompt: document 8 of 8, code 8 of 8.

    A prompt with nothing to state is returned byte-identical.
    """
    parts: list[str] = []
    if text_shape_instruction:
        parts.append(text_shape_instruction)
    parts.append(prompt)
    if acceptance_directives:
        parts.append(acceptance_directives)
    return "\n\n".join(parts)
