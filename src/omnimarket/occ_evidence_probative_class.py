# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probative class of a ``dod_evidence`` ``check_value`` — the surrogate predicate.

A ``dod_evidence`` item has two independent surfaces: the **description** (prose
a human reads to decide whether the bar was met) and the **check_value** (the
command a machine runs). Nothing asserted that they describe the same proof, so
a command that is genuinely executed and genuinely falsifiable could stand in
for a ticket-specific proof it has nothing to do with — admissible, green, and
vacuous. Every gate reads only the command, and every gate's question is "is
this command real?", never "does this command have anything to do with this
ticket?".

This module answers a strictly narrower question that needs no judgement and no
prose:

    **Is this command's exit status a function of the product change at all?**

A command whose exit status is invariant over the product diff cannot bear a
verdict about the product. It is *provenance* — a true statement about which PR
exists, or about a foreign suite being intact — and it is admitted as such. It
is never *proof*, so it must not count toward completion.

Two classes are recognised. Both are exit-status-invariant **by construction**,
not by heuristic:

``PR_STATE_SURROGATE``
    A bare ``gh pr view <ref> --repo <repo> --json <fields>`` with nothing
    attached that could turn a field value into a verdict. ``gh pr view``
    exits 0 iff the PR is visible to the token; the ``--json`` selection only
    changes what is printed to stdout, which nothing reads. It is green for
    every PR on GitHub, before and after the product change, and it is green
    with the entire product fix reverted.

``FOREIGN_SUITE_SURROGATE``
    A pytest invocation of an enumerated generic suite that is
    ticket-independent by construction — currently only OCC's own
    ``tests/test_evidence_admissibility.py``, which tests the admissibility
    predicate rather than any ticket. The producer mints it unconditionally on
    every companion (see ``occ_evidence_stamp.ADMISSIBILITY_VALIDATOR_*``) to
    stop a companion being born BLOCKED by ``_has_effective_check``; that is a
    legitimate job, and it is provenance, not proof of the ticket.

**Deliberately OUT of scope, recorded rather than silently over-reached.** An
*asserted* PR-state probe — ``gh pr view <n> --json state --jq '.state' | grep
-q MERGED`` — is also a proof about PR state rather than about behaviour, but it
CAN go red, and for a ticket whose DoD genuinely is "the PR merged" it is the
correct evidence. Separating those two readings needs the ticket's own bar,
which is the OMN-14409 (self-certifying DoD) problem, not this one. This module
classifies only what is vacuous by construction, so a caller can act on its
verdict without a judgement call.

**Direction of error.** Every ambiguity resolves to ``PROBATIVE`` (no demotion).
A command this module cannot parse, or that carries any shell operator that
could attach an assertion, is left alone. Callers demote a *verified* result to
non-probative and never touch a *failed* one, so applying this predicate is
monotone toward refusal: it can subtract a green, and it can never manufacture
one.

Pure — no I/O, no network, no clock.
"""

from __future__ import annotations

import re
from enum import StrEnum

__all__: list[str] = [
    "FOREIGN_SUITE_DENYLIST",
    "EnumEvidenceProbativeClass",
    "classify_check_value",
    "is_surrogate_check_value",
    "surrogate_refusal_reason",
]


class EnumEvidenceProbativeClass(StrEnum):
    """Whether a ``check_value``'s exit status can depend on the product change."""

    #: The command's exit status may depend on the product change (or this
    #: module cannot prove otherwise). The default, and the only verdict that
    #: counts toward completion.
    PROBATIVE = "probative"

    #: A bare ``gh pr view`` — green for every visible PR, with or without the
    #: product change.
    PR_STATE_SURROGATE = "pr_state_surrogate"

    #: A ticket-independent generic suite standing in for a ticket-specific
    #: proof.
    FOREIGN_SUITE_SURROGATE = "foreign_suite_surrogate"


#: Enumerated generic suites that can never be ticket-specific proof for a
#: FOREIGN ticket. Ratcheted by enumeration on purpose (the ticket's candidate
#: mechanism 2): a pattern like "any pytest path outside the product diff" would
#: sweep in the legitimately-generic repo-wide invariant suites a ticket really
#: did change, which is exactly the negative control this must not break.
FOREIGN_SUITE_DENYLIST: frozenset[str] = frozenset(
    {
        # OCC's own OMN-15309 admissibility-predicate suite. Tests the predicate,
        # never a ticket. Minted unconditionally by the OCC companion producer.
        "tests/test_evidence_admissibility.py",
    }
)

# Shell metacharacters that can attach an assertion to a command, turning an
# exit-status-invariant probe into a falsifiable one (``| grep -q``, ``&& test``,
# ``$(...)`` substitution, a redirect a later leg reads). ``$`` is deliberately
# NOT here: ``${PR_NUMBER}`` is a VALUE placeholder the runner substitutes, not
# an operator — command substitution ``$(`` is matched separately below.
_ASSERTION_METACHARS: frozenset[str] = frozenset("|&;<>`\n")

_COMMAND_SUBSTITUTION = re.compile(r"\$\(")

# ``gh pr view`` flags that route the PR's field values into a program whose own
# exit status becomes the verdict. Their presence means the command may be
# falsifiable, so it is left PROBATIVE.
_GH_ASSERTING_FLAGS: frozenset[str] = frozenset({"--jq", "-q", "--template", "-t"})

_PYTEST_INVOCATION = re.compile(r"(?:^|\s)(?:-m\s+)?pytest(?:\s|$)")


def _carries_an_assertion(command: str) -> bool:
    """True when anything in ``command`` could make its exit status falsifiable.

    Conservative by design — a false ``True`` here only means "do not classify
    as a surrogate", which is the safe direction.
    """
    if any(char in _ASSERTION_METACHARS for char in command):
        return True
    return bool(_COMMAND_SUBSTITUTION.search(command))


def _is_bare_gh_pr_view(command: str) -> bool:
    """True for a ``gh pr view`` whose exit status depends only on PR visibility.

    ``gh pr view`` prints the requested fields and exits 0 iff the PR resolves;
    ``--json`` never changes the exit status. With no assertion attached and no
    ``--jq``/``--template`` handing the values to another program, the exit
    status is a function of the PR existing — not of anything the PR contains.
    """
    tokens = command.split()
    if tokens[:3] != ["gh", "pr", "view"]:
        return False
    if any(token in _GH_ASSERTING_FLAGS for token in tokens):
        return False
    # ``--jq=<expr>`` / ``--template=<expr>`` attached-value spellings.
    return not any(
        token.split("=", 1)[0] in _GH_ASSERTING_FLAGS
        for token in tokens
        if "=" in token
    )


def _is_foreign_suite(command: str) -> bool:
    """True when the command's whole payload is a denylisted generic suite."""
    if not _PYTEST_INVOCATION.search(command):
        return False
    return any(suite in command for suite in FOREIGN_SUITE_DENYLIST)


def classify_check_value(check_value: str | None) -> EnumEvidenceProbativeClass:
    """Return the probative class of one ``check_value``.

    ``None``, a non-string, or an empty/whitespace-only value is ``PROBATIVE``:
    a malformed check is a separate defect that the collector's own shape guards
    already hard-fail, and this module must never be the thing that turns a
    malformed check into a quietly-tolerated one.
    """
    if not isinstance(check_value, str):
        return EnumEvidenceProbativeClass.PROBATIVE
    command = check_value.strip()
    if not command:
        return EnumEvidenceProbativeClass.PROBATIVE

    if _carries_an_assertion(command):
        return EnumEvidenceProbativeClass.PROBATIVE

    if _is_bare_gh_pr_view(command):
        return EnumEvidenceProbativeClass.PR_STATE_SURROGATE

    if _is_foreign_suite(command):
        return EnumEvidenceProbativeClass.FOREIGN_SUITE_SURROGATE

    return EnumEvidenceProbativeClass.PROBATIVE


def is_surrogate_check_value(check_value: str | None) -> bool:
    """True when ``check_value`` cannot bear a verdict about the product change."""
    return classify_check_value(check_value) is not EnumEvidenceProbativeClass.PROBATIVE


def surrogate_refusal_reason(
    probative_class: EnumEvidenceProbativeClass, check_value: str
) -> str:
    """Return the operator-facing reason a surrogate does not count as proof.

    Names the class, the command, and what the command actually proves, so the
    gap comment a caller writes is actionable without re-deriving the analysis.
    """
    what_it_proves = {
        EnumEvidenceProbativeClass.PR_STATE_SURROGATE: (
            "that the pull request is visible to the token — it exits 0 for "
            "every PR on GitHub, and it is green with the product change "
            "entirely reverted"
        ),
        EnumEvidenceProbativeClass.FOREIGN_SUITE_SURROGATE: (
            "that a generic, ticket-independent suite is intact — it runs no "
            "code this ticket changed and goes red only if that foreign suite "
            "breaks"
        ),
    }.get(probative_class, "nothing about this ticket")
    return (
        f"NON_PROBATIVE[{probative_class.value}]: {check_value!r} proves "
        f"{what_it_proves}. Admitted as provenance; it does NOT count toward "
        "completion. Bind a check whose exit status depends on the product "
        "change (a content read at a pinned ref, or a test that this ticket's "
        "diff makes pass)."
    )
