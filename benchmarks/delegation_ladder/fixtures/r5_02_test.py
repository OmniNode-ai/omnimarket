"""Proof fixture for the R5 rung. DO NOT change the import.

This module is never collected in place. The trace generator copies the
DEFECTIVE copy of the function to `subject.py` in a scratch directory and runs
this file beside it, so `from subject import ...` is what binds it to the
mutant. Repointing the import at the committed subject module makes these
tests PASS, which means the committed failure trace can never be regenerated
and the R5 tasks lose the evidence they are built on.

`tests/test_scorers.py` regenerates every trace from this file plus the
committed mutant and asserts it still matches the committed bytes, so this
is enforced rather than merely requested.
"""

from subject import detect_add_remove_conflicts


def test_differently_cased_values_conflict_by_default() -> None:
    assert detect_add_remove_conflicts(["Foo"], ["foo"], "handlers") == ["foo"]


def test_disjoint_lists_do_not_conflict() -> None:
    assert detect_add_remove_conflicts(["foo"], ["bar"], "handlers") == []
