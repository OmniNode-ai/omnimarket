# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A published code case that must complete names the artifact it asks for.

The code_generation deterministic floor holds ``final_artifact_only`` as a
blocking rule: an answer carrying prose outside its code block is a
TASK_MISMATCH, and the case climbs off the local tier. So a case that expects
``terminal: completed`` decides its own verdict by how it asks. A prompt that
names one concrete function is answered with that function and nothing else; a
prompt that describes an invariant and asks for nothing in particular is
answered with an explanation instead of, or around, the code about as often as
not.

Measured on the nightly's proof lane, 2026-10-03 to 2026-10-11, over the 17
nightly runs that reached it: I3 (``Write a Python function `add(a, b)` ...``)
passed the floor on its first answer 17 of 17 times, on both local backends the
spread picks between. I9 (``(cross-cutting) every completed row has non-empty
model_name + ...``) failed the floor on its first answer 10 of 17 times, on both
backends, with ``contains no code artifact`` or ``non-artifact prose outside
code block``; 8 of those 17 runs climbed to the free cloud rungs, failed there
too and terminalised ``failed``. Run 38122289336 (2026-10-11) is one of them,
and it held every runtime PR red on the Delegation Health Check. Nothing about
the delegation path regressed on those nights; the case flipped a coin.

I9's own assertion is the runner's cross-cutting telemetry check over every
completed row. Its published task only has to complete, so it asks for a named
function like I3 does. This rule keeps the next such case from being written
the other way. An xfail case is exempt: its red is already accounted for, and
the ratchet in ``test_xfail_ratchet_omn19446.py`` holds it to its stage.
"""

from __future__ import annotations

import re

import pytest

from tests.delegation_golden.corpus_loader import (
    ModelCorpus,
    ModelCorpusCase,
    ModelExpected,
    load_corpus,
)

pytestmark = pytest.mark.unit

# A backticked Python call signature: `name(args)`.
_NAMED_ARTIFACT = re.compile(r"`[A-Za-z_][A-Za-z0-9_]*\([^`]*\)`")

_CODE_TASK_TYPES = frozenset({"code_generation"})


def unnamed_code_cases(corpus: ModelCorpus) -> list[str]:
    """Ids of published code cases expected to complete whose prompt names no artifact."""
    return [
        case.id
        for case in corpus.integration_cases()
        if case.task_type in _CODE_TASK_TYPES
        and case.expected.terminal == "completed"
        and case.xfail is None
        and not _NAMED_ARTIFACT.search(case.prompt)
    ]


def _corpus_with(prompt: str) -> ModelCorpus:
    return ModelCorpus(
        schema_version="1.0.0",
        corpus_version="control",
        ticket="OMN-18349",
        cases=(
            ModelCorpusCase(
                id="CONTROL",
                layer="integration",
                task_type="code_generation",
                prompt=prompt,
                acceptance_criteria=("response_non_empty",),
                expected=ModelExpected(terminal="completed"),
            ),
        ),
    )


def test_every_completing_code_case_names_its_artifact() -> None:
    """No published code case that must complete leaves its artifact unnamed."""
    assert unnamed_code_cases(load_corpus()) == []


def test_control_the_old_i9_prompt_is_flagged() -> None:
    """CONTROL: the prompt that terminalised failed 8 of 17 nights is refused."""
    old_i9 = (
        "(cross-cutting) every completed row has non-empty model_name + "
        "tokens_input>0 + tokens_output>0"
    )
    assert unnamed_code_cases(_corpus_with(old_i9)) == ["CONTROL"]


def test_control_the_i3_prompt_is_admitted() -> None:
    """CONTROL: the prompt that passed the floor 17 of 17 nights is admitted."""
    i3 = "Write a Python function `add(a, b)` that returns the sum of two integers."
    assert unnamed_code_cases(_corpus_with(i3)) == []
