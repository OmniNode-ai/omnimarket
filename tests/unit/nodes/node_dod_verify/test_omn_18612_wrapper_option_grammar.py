# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18612 — a wrapper option that consumes a value is not the command head.

THE DEFECT, MEASURED
--------------------
``_segment_head_and_args`` finds a segment's real command head by skipping
wrapper words. Before this change it had no notion of an option that consumes
the NEXT token, so the token after such an option was returned as the head.

Measured against ``origin/dev`` on OMN-17276's own behaviour proof:

* ``uv run pytest <file> -q`` -- BEHAVIOR
* ``uv run --with-requirements <req> --with-requirements <req> pytest <file>
  -q`` -- INDETERMINATE

The head of the second resolves to ``docker/onex-api/requirements.txt``.

WHY IT COSTS SOMETHING
----------------------
The second spelling exists BECAUSE the first could not execute at its declared
cwd: omninode_infra's root project deliberately omits the driver the service
needs, so the item was superseded by one installing the service's own two
requirements files — the same pair the service's CI workflow installs. It runs
green (27 passed in 10.63s in the diagnose block of sweep run 35249003165) and
that same block reports ``behavior_proving=0``. The repair made the proof
runnable and, in the same stroke, made it uncountable. The auto-close flip
predicate is a conjunction requiring at least one behaviour-proving check, so
the tally was the only unmet conjunct on that contract.

WHY THIS IS NOT A PERMISSIVE WIDENING, which is the half worth pinning
---------------------------------------------------------------------
The module fails closed on purpose: a misclassification toward BEHAVIOR
releases an automated flip, one away from it merely holds one. Two properties
keep the table on the safe side of that, and both are pinned below.

* A grammar is consulted only after its own wrapper word has been seen, so a
  bare ``-u`` or ``--with`` at the head of some other command is still
  unrecognised.
* An option absent from its wrapper's grammar is NOT skipped. Skipping unknown
  options would let ``uv run --with pytest ruff check src/`` resolve its head
  to ``pytest`` — the name of an INSTALLED PACKAGE, not of the program being
  run — and call a lint run a behaviour proof.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.services.check_proof_class import (
    classify_check,
    classify_command,
)

pytestmark = pytest.mark.unit

# OMN-17276's active Group A behaviour proof, verbatim from
# contracts/OMN-17276.yaml on onex_change_control@dev.
_OMN_17276_COMMAND = (
    "uv run --with-requirements docker/onex-api/requirements.txt "
    "--with-requirements docker/onex-api/requirements-test.txt "
    "pytest docker/onex-api/tests/test_omn_17276_typed_refusal_codes.py -q"
)
_OMN_17276_COMMAND_WITHOUT_OPTIONS = (
    "uv run pytest docker/onex-api/tests/test_omn_17276_typed_refusal_codes.py -q"
)


# --------------------------------------------------------------------------
# AC1 — the ticket's own command, with its option-free spelling as the
# positive control that the easy case already worked.
# --------------------------------------------------------------------------


def test_the_omn_17276_behaviour_proof_is_classed_behavior() -> None:
    assert classify_command(_OMN_17276_COMMAND) is EnumCheckProofClass.BEHAVIOR


def test_the_option_free_spelling_is_the_positive_control() -> None:
    """The classifier was already returning BEHAVIOR for the easy case.

    Without this the AC1 assertion above could pass on a classifier that
    returns BEHAVIOR for everything, which is the failure this whole module
    is built to refuse.
    """
    assert (
        classify_command(_OMN_17276_COMMAND_WITHOUT_OPTIONS)
        is EnumCheckProofClass.BEHAVIOR
    )


def test_the_contract_item_classifies_through_classify_check() -> None:
    """The entry point the sweep actually calls, not only the inner function."""
    assert (
        classify_check({"check_type": "command", "command": _OMN_17276_COMMAND})
        is EnumCheckProofClass.BEHAVIOR
    )


# --------------------------------------------------------------------------
# AC2 — the attached spelling is the same option and must agree.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("separated", "attached"),
    [
        (
            "uv run --with-requirements reqs.txt pytest tests/x.py -q",
            "uv run --with-requirements=reqs.txt pytest tests/x.py -q",
        ),
        (
            "uv run --python 3.13 pytest tests/x.py -q",
            "uv run --python=3.13 pytest tests/x.py -q",
        ),
        (
            "uv run --directory omnibase_infra pytest tests/x.py -q",
            "uv run --directory=omnibase_infra pytest tests/x.py -q",
        ),
        (
            "env --unset PYTHONPATH uv run pytest tests/x.py -q",
            "env --unset=PYTHONPATH uv run pytest tests/x.py -q",
        ),
    ],
)
def test_attached_and_separated_option_values_agree(
    separated: str, attached: str
) -> None:
    """``--name=value`` and ``--name value`` are one option, not two grammars."""
    assert classify_command(separated) is classify_command(attached)
    assert classify_command(attached) is EnumCheckProofClass.BEHAVIOR


@pytest.mark.parametrize(
    "command",
    [
        "uv run --with httpx --with-requirements reqs.txt pytest tests/x.py -q",
        "uv run --isolated --with-requirements reqs.txt pytest tests/x.py -q",
        "uv run --frozen --no-project pytest tests/x.py -q",
        "nice -n 10 uv run pytest tests/x.py -q",
        "sudo -u runner uv run pytest tests/x.py -q",
        "env -u PYTHONPATH uv run --with-requirements reqs.txt pytest tests/x.py -q",
        "cd docker/onex-api && uv run --with-requirements reqs.txt pytest tests/x.py",
    ],
)
def test_wrapper_options_resolve_through_to_the_runner(command: str) -> None:
    """Boolean and value-taking options, stacked, and behind other prefixes."""
    assert classify_command(command) is EnumCheckProofClass.BEHAVIOR


# --------------------------------------------------------------------------
# AC3 — the negative-control corpus. None of these may become BEHAVIOR.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # The ticket names both explicitly. A bare interpreter running an
        # arbitrary script is not self-evidently a test run; OMN-17276's own
        # second check is this shape and was deliberately NOT claimed as a
        # defect.
        (
            "python3 scripts/probe_typed_refusals.py",
            EnumCheckProofClass.INDETERMINATE,
        ),
        (
            "gh pr view 3695 --repo OmniNode-ai/omnibase_infra --json state",
            EnumCheckProofClass.SURROGATE,
        ),
        # THE control for this change. `--with` names an installed package,
        # not the program being run: skipping it would hand back `pytest` as
        # the head and call a lint run a behaviour proof.
        (
            "uv run --with pytest ruff check src/",
            EnumCheckProofClass.INDETERMINATE,
        ),
        (
            "uv run --with-requirements reqs.txt ruff format --check src/",
            EnumCheckProofClass.INDETERMINATE,
        ),
        # The set is closed: an option absent from its wrapper's grammar
        # still ends the walk rather than being skipped on a guess.
        (
            "uv run --some-future-option pytest tests/x.py -q",
            EnumCheckProofClass.INDETERMINATE,
        ),
        # A grammar is only live once its own wrapper word has been seen.
        (
            "--with-requirements reqs.txt pytest tests/x.py",
            EnumCheckProofClass.INDETERMINATE,
        ),
        ("-u PYTHONPATH pytest tests/x.py", EnumCheckProofClass.INDETERMINATE),
        # Everything OMN-18135 refused, still refused.
        ('bash -c "uv run pytest tests/x.py -q"', EnumCheckProofClass.INDETERMINATE),
        ("ssh host uv run pytest tests/x.py -q", EnumCheckProofClass.INDETERMINATE),
        (
            "uv run --with-requirements reqs.txt pytest tests/x.py -q || true",
            EnumCheckProofClass.INDETERMINATE,
        ),
    ],
)
def test_negative_controls_are_not_behavior(
    command: str, expected: EnumCheckProofClass
) -> None:
    assert classify_command(command) is expected


def test_a_wrapper_with_only_options_still_proves_nothing() -> None:
    """Consuming every token must fall through, not read the last one as a head."""
    assert classify_command("uv run --isolated") is EnumCheckProofClass.INDETERMINATE
    assert (
        classify_command("uv run --with-requirements reqs.txt")
        is EnumCheckProofClass.INDETERMINATE
    )
    assert classify_command("env -i") is EnumCheckProofClass.INDETERMINATE


def test_a_denylisted_foreign_suite_stays_a_surrogate_behind_options() -> None:
    """The OMN-15391 corpus is checked first and the table must not bypass it."""
    assert (
        classify_command(
            "uv run --with-requirements reqs.txt pytest "
            "tests/test_evidence_admissibility.py -q"
        )
        is EnumCheckProofClass.SURROGATE
    )
