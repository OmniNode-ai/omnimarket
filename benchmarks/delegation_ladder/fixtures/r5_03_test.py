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

from subject import validate_import_path_format


def test_single_segment_path_is_rejected() -> None:
    valid, message = validate_import_path_format("singlemodule")
    assert valid is False
    assert message == "Import path must include module and class (at least 2 segments)"


def test_two_segment_path_is_accepted() -> None:
    assert validate_import_path_format("mypackage.Handler") == (True, None)
