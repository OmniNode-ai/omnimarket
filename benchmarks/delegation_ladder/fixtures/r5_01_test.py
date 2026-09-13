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

from subject import is_valid_onex_name


def test_empty_name_is_rejected() -> None:
    assert is_valid_onex_name("") is False


def test_snake_case_name_is_accepted() -> None:
    assert is_valid_onex_name("http_client") is True
