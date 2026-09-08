# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18039 — a namespaced npm script is still a test run.

``_BEHAVIOR_PAIRS`` pairs a runner with a subcommand and matched it by exact
membership::

    if head == runner and subcommand in args:

``pnpm test:audit-verdict`` produces ``args == ["test:audit-verdict"]``, and
``"test" in ["test:audit-verdict"]`` is False, so the command fell through every
branch and failed closed to ``INDETERMINATE``. Namespaced scripts (``test:unit``,
``test:ci``, ``test:e2e``) are the dominant npm convention, so **no JavaScript or
TypeScript surface using one could earn** ``BEHAVIOR`` — however real its test.

That is not merely cosmetic: ``node_dod_verify``'s flip rule (OMN-15911) needs at
least one behavior-proving check, so a contract whose only executing check is a
namespaced script reports ``behavior_proving_count: 0`` and the OMN-16106
autoclose sweep can post ``gap_no_behavior_proof`` against a ticket that carries a
genuine, executed proof. Live on OMN-17863, whose
``dod-omn17863-audit-verdict-behavior-proof`` item runs ``pnpm test:audit-verdict``
16/16 and still recorded ``proof_class: indeterminate``.

The repair matches the subcommand exactly **or** as a ``<subcommand>:<name>``
namespace. The distinction that matters is against a bare prefix: a
``startswith("test")`` test would also swallow ``pnpm test-nothing-here``, which is
why the ticket's AC3 names that command specifically. The allowlist stays tight
and positive per the module's own docstring rule 3, and rule 2's fail-closed
direction is untouched — this only ever moves a command toward BEHAVIOR, so no
flip that is held today can be released by accident elsewhere.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.services.check_proof_class import classify_check

pytestmark = pytest.mark.unit


def _classify(command: str) -> EnumCheckProofClass:
    return classify_check({"check_type": "command", "check_value": command})


# AC1 — the defect. Every one of these returned INDETERMINATE before the fix.
@pytest.mark.parametrize(
    "command",
    [
        "pnpm test:audit-verdict",
        "npm test:unit",
        "yarn test:ci",
        "pnpm test:e2e",
        "bun test:integration",
    ],
)
def test_namespaced_test_scripts_classify_behavior(command: str) -> None:
    """``test:<name>`` executes a real suite and must prove behavior."""
    assert _classify(command) is EnumCheckProofClass.BEHAVIOR


# The `run` verb is the same script by a longer road. Called out separately
# because the ticket flags it as a decision to take deliberately rather than
# inherit: approved 2026-09-08.
@pytest.mark.parametrize(
    "command",
    ["pnpm run test:audit-verdict", "npm run test:unit", "yarn run test:ci"],
)
def test_the_explicit_run_verb_is_also_behavior(command: str) -> None:
    """``pnpm run test:x`` executes exactly what ``pnpm test:x`` does."""
    assert _classify(command) is EnumCheckProofClass.BEHAVIOR


# AC2 — no regression on what already worked.
@pytest.mark.parametrize(
    "command",
    [
        "pnpm test",
        "npm test",
        "yarn test",
        "go test ./...",
        "cargo test",
        "vitest run",
        "uv run pytest tests/",
    ],
)
def test_plain_test_invocations_still_classify_behavior(command: str) -> None:
    """The exact-match leg keeps working; the change only adds a second leg."""
    assert _classify(command) is EnumCheckProofClass.BEHAVIOR


# AC3 — the widening is bounded to a test-script shape. This is the criterion a
# prefix match on "test" alone would fail, and `test-nothing-here` is the case
# that separates the two: it starts with "test" and is not one.
@pytest.mark.parametrize(
    "command",
    [
        "pnpm build",
        "pnpm lint",
        "pnpm deploy",
        "pnpm test-nothing-here",
        "pnpm run build",
        "npm run deploy",
        "yarn testify",
        "pnpm testing",
    ],
)
def test_non_test_scripts_never_classify_behavior(command: str) -> None:
    """A script is not a test because its name begins with the letters t-e-s-t."""
    assert _classify(command) is not EnumCheckProofClass.BEHAVIOR


# AC4 — fail-closed is preserved: unrecognized stays INDETERMINATE, not BEHAVIOR.
@pytest.mark.parametrize(
    "command",
    ["pnpm exec something-else", "frobnicate test:unit", "pnpm"],
)
def test_unrecognized_shapes_still_fail_closed(command: str) -> None:
    """Rule 2: anything unrecognized is INDETERMINATE, never BEHAVIOR."""
    assert _classify(command) is EnumCheckProofClass.INDETERMINATE


def test_the_colon_is_what_distinguishes_a_namespace_from_a_lookalike() -> None:
    """The single assertion that pins the repair against a bare prefix match.

    Both commands begin with ``test``; only one is a namespaced script. A
    ``startswith("test")`` implementation passes every other test in this file
    and fails this one, which is the whole point of stating it separately.
    """
    assert _classify("pnpm test:unit") is EnumCheckProofClass.BEHAVIOR
    assert _classify("pnpm test-nothing-here") is not EnumCheckProofClass.BEHAVIOR
