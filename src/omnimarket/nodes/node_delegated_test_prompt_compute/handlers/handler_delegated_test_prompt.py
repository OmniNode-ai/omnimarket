# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegatedTestPrompt — the WRITE and REPAIR prompts of the delegated
test loop (OMN-19361, task T5).

Pure definition-B compute: ``handle(request: ModelDelegatedTestPromptRequest)
-> ModelDelegatedTestPromptBundle``. No I/O, no envelope type.

The repair prompt follows ``node_schema_repair_compute``'s shape: the thing the
model produced, what was wrong with it, and one instruction. What is wrong
comes from the pytest failure digest, never from a model's own account.

The bundle is refused, not trimmed, when any forbidden fragment appears
anywhere in it. The caller fills that list with the hidden human test's class
and function names; a prompt that carried one would let the model copy the
answer instead of writing it, and the loop's verdict would measure nothing.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegated_test_prompt_compute.models.model_delegated_test_prompt import (
    MAX_EXCERPT_CHARS,
    MAX_PREVIOUS_TEST_CHARS,
    ModelDelegatedTestPromptBundle,
    ModelDelegatedTestPromptRequest,
)

RESPONSE_CONTRACT: dict[str, object] = {
    "type": "object",
    "required": ["test_path", "test_source"],
    "properties": {
        "test_path": {"type": "string"},
        "test_source": {"type": "string"},
    },
    "additionalProperties": False,
}


class DelegatedTestPromptRefusedError(ValueError):
    """The bundle carried a forbidden fragment."""


def _module_name(target_path: str) -> str:
    path = target_path.removeprefix("src/")
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n# ... truncated ...\n", True


def build_prompt_bundle(
    request: ModelDelegatedTestPromptRequest,
) -> ModelDelegatedTestPromptBundle:
    excerpt, truncated = _cap(request.target_excerpt, MAX_EXCERPT_CHARS)
    lines = [
        "You write one pytest test module that checks a single acceptance "
        "criterion against the code shown below.",
        "",
        "Acceptance criterion:",
        request.criterion.strip(),
        "",
        f"Code under test: `{request.target_path}` "
        f"(import it as `{_module_name(request.target_path)}`):",
        "```python",
        excerpt.rstrip(),
        "```",
        "",
        "Rules:",
        "- The test must PASS against the code shown above, exactly as it is.",
        "- The test must FAIL if the behaviour the criterion describes were broken "
        "or missing. Assert the behaviour itself (the exception type raised, the "
        "value returned, the warning emitted or not emitted), not merely that "
        "something runs.",
        "- Use only the Python standard library, pytest, and the package under test. "
        "No network access, no files outside pytest's tmp_path, no environment "
        "secrets.",
        "- Do not skip or xfail anything. Test function names start with `test_`.",
        f"- The module is written to `{request.test_path}`.",
    ]
    if request.mode == "repair" and request.failure is not None:
        previous, _ = _cap(request.previous_test, MAX_PREVIOUS_TEST_CHARS)
        failure = request.failure
        lines += [
            "",
            "Your previous test module:",
            "```python",
            previous.rstrip(),
            "```",
            "",
            "It was run against the code above and did not pass:",
            f"- outcome: {failure.outcome}",
            f"- failing test: {failure.failing_node_id or 'n/a'}",
            f"- exception: {failure.exception_type or 'n/a'}",
            f"- message: {failure.message or 'n/a'}",
            "- traceback tail:",
            "```",
            failure.frames.rstrip() or "n/a",
            "```",
        ]
        if failure.outcome == "failed_collection":
            lines.append(
                "The module did not import. Check every imported name against the "
                "code shown above."
            )
        lines.append(
            "Fix the test so it passes against the code shown above while still "
            "checking the criterion."
        )
    lines += [
        "",
        "Reply with ONLY a JSON object and nothing else, in this shape:",
        f'{{"test_path": "{request.test_path}", "test_source": "<the complete '
        'Python module as one JSON string>"}',
    ]
    prompt = "\n".join(lines) + "\n"

    for fragment in request.forbidden_fragments:
        if fragment and fragment in prompt:
            raise DelegatedTestPromptRefusedError(
                f"the prompt carries a forbidden fragment ({len(fragment)} chars); refused"
            )
    return ModelDelegatedTestPromptBundle(
        prompt=prompt,
        test_path=request.test_path,
        response_contract=dict(RESPONSE_CONTRACT),
        excerpt_truncated=truncated,
    )


class HandlerDelegatedTestPrompt:
    """COMPUTE handler: criterion, target and last failure in, one prompt out."""

    def handle(
        self, request: ModelDelegatedTestPromptRequest
    ) -> ModelDelegatedTestPromptBundle:
        return build_prompt_bundle(request)


__all__ = [
    "RESPONSE_CONTRACT",
    "DelegatedTestPromptRefusedError",
    "HandlerDelegatedTestPrompt",
    "build_prompt_bundle",
]
