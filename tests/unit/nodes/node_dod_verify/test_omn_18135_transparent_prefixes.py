# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18135 — the command head resolves through transparent prefixes.

THE DEFECT, MEASURED
--------------------
Three separate lanes hit the same wall in one window, and it is one defect
with two surfaces.

``classify_command`` splits a command into pipeline segments and walks them.
It returns ``INDETERMINATE`` on the **first** segment whose head is not
behaviour, merge-state or static-inspection, and never looks at the segments
that follow. ``_segment_head_and_args`` resolves that head by skipping env
assignments and a fixed wrapper list, then returning the next literal token
whatever it is.

So, measured against the classifier at ``origin/dev`` before this change:

* ``uv run pytest tests/x.py -q`` -- behavior
* ``env PYTHONPATH=/x uv run pytest tests/x.py -q`` -- behavior
* ``env -u PYTHONPATH uv run pytest tests/x.py -q`` -- INDETERMINATE
* ``env -i uv run pytest tests/x.py -q`` -- INDETERMINATE
* ``cd $OMNI_HOME/omnibase_infra && uv run pytest tests/x.py -q`` -- INDETERMINATE

``env -u PYTHONPATH`` resolves to the head ``-u``. ``cd`` is in none of the
three recognised sets, so it short-circuits the scan before the runner is
ever seen. Both are the same mistake: a prefix that says WHERE or WITH WHAT
ENVIRONMENT a command runs is being read as WHAT the command proves.

WHY IT COSTS SOMETHING
----------------------
``env -u PYTHONPATH`` is the form this org's own doctrine prescribes for
clearing an ambient ``PYTHONPATH`` before invoking a venv interpreter, and
``cd <dir> &&`` is the ordinary way a contract points a check at a sibling
repo. A contract author who writes either gets ``behavior_proving_count``
zero and a ``gap_no_behavior_proof`` hold on a check that genuinely runs the
product's tests.

WHAT A TRANSPARENT PREFIX IS, stated so the set cannot quietly grow
-------------------------------------------------------------------
A prefix is transparent when it can change where, or with what environment, a
command runs, and can never change whether the command's exit status depends
on the product diff. Exactly three are transparent:

* a bare ``NAME=value`` assignment (already was),
* ``env``, now including its option words (``-i``, ``-0``, ``--null``,
  ``--ignore-environment``, ``-u NAME`` / ``--unset=NAME``, ``-C DIR`` /
  ``--chdir=DIR``),
* ``cd <dir>`` as a whole segment.

Everything else still fails closed to ``INDETERMINATE``, which is the
direction the module's own docstring argues for: a misclassification toward
BEHAVIOR releases an automated flip, one away from it merely holds one.

``cd`` is transparent rather than static-inspection on purpose. Calling it a
static inspection would make ``cd x`` alone a SURROGATE — a verdict about a
command that inspects nothing and asserts nothing.

NOT A GATE CONFLICT, and the correction is recorded here because a lane was
about to act on it
-------------------------------------------------------------------------
Lane ``omn17557-closure`` reported that receipt hardening and this classifier
are mutually exclusive: hardening refuses a bare ``uv run pytest`` receipt as
commit-sha-unresolvable because no repo is named, and adding the anchor was
believed to reclassify the check to ``indeterminate``.

The anchor is not what breaks it. ``gh api repos/<owner>/<repo>/commits/<sha>
--jq .sha && uv run pytest ...`` classifies **behavior** today and after this
change: the ``gh api`` segment is merge-state, the walk continues, and it
reaches the runner. ``cd`` was the culprit. A form satisfying both gates
therefore already existed, and it is the one hardening's own error text
recommends. Pinned below so the correction cannot be lost.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.services.check_proof_class import (
    classify_command,
)

pytestmark = pytest.mark.unit

_PYTEST = "uv run pytest tests/test_thing.py -q"
_ANCHOR = "gh api repos/OmniNode-ai/omnibase_infra/commits/b370ab69 --jq .sha"


# --------------------------------------------------------------------------
# AC1 / AC6 — the three shapes that were downgraded.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        f"env -u PYTHONPATH {_PYTEST}",
        f"env --unset=PYTHONPATH {_PYTEST}",
        f"env -i {_PYTEST}",
        f"env --ignore-environment {_PYTEST}",
        f"env -u PYTHONPATH -u VIRTUAL_ENV {_PYTEST}",
        f"env -u PYTHONPATH PYTEST_ADDOPTS=-x {_PYTEST}",
    ],
)
def test_env_options_are_transparent(command: str) -> None:
    """`env` clearing or replacing the environment still runs the runner.

    The losing form is the one this org's doctrine prescribes, which is what
    makes this worth fixing rather than documenting.
    """
    assert classify_command(command) is EnumCheckProofClass.BEHAVIOR


@pytest.mark.parametrize(
    "command",
    [
        f"cd omnibase_infra && {_PYTEST}",
        f"cd ${{OMNI_HOME}}/omnibase_infra && {_PYTEST}",
        f"cd ${{OMNI_HOME}}/omnibase_infra && {_ANCHOR} && {_PYTEST}",
        f"cd omnibase_infra; {_PYTEST}",
    ],
)
def test_cd_is_transparent_and_does_not_short_circuit(command: str) -> None:
    """A `cd` segment must not end the walk before the runner is reached.

    The third and fourth cases are the ones a real contract writes: point the
    check at a sibling repo, optionally anchor the commit for receipt
    hardening, then run the tests.
    """
    assert classify_command(command) is EnumCheckProofClass.BEHAVIOR


def test_a_cd_only_command_asserts_nothing_and_says_so() -> None:
    """`cd x` on its own proves nothing and must not become a SURROGATE.

    This is why `cd` is transparent rather than static-inspection: a
    surrogate verdict is a claim that something was inspected, and nothing
    was. With every segment transparent there is no evidence either way, so
    the fail-closed answer is the honest one.
    """
    assert classify_command("cd omnibase_infra") is EnumCheckProofClass.INDETERMINATE
    assert classify_command("env -i") is EnumCheckProofClass.INDETERMINATE


# --------------------------------------------------------------------------
# AC8 — the correction: the two gates were never mutually exclusive.
# --------------------------------------------------------------------------


def test_the_repo_qualified_anchor_never_downgraded_anything() -> None:
    """The form receipt hardening asks for keeps its behaviour classification.

    Pinned because a lane was about to restructure contracts on the belief
    that it did not. `gh api repos/...` is a merge-state read, so the walk
    continues past it and reaches the runner. This passed before this change
    and must keep passing after it.
    """
    assert classify_command(f"{_ANCHOR} && {_PYTEST}") is EnumCheckProofClass.BEHAVIOR
    assert classify_command(f"{_ANCHOR}; {_PYTEST}") is EnumCheckProofClass.BEHAVIOR


# --------------------------------------------------------------------------
# AC2 / AC7 — the controls. Transparency is a closed set, and widening it
# is what this suite exists to make expensive.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (_PYTEST, EnumCheckProofClass.BEHAVIOR),
        (f"PYTHONPATH= {_PYTEST}", EnumCheckProofClass.BEHAVIOR),
        (f"env PYTHONPATH=/x {_PYTEST}", EnumCheckProofClass.BEHAVIOR),
        ("onex run-node node_x --input y", EnumCheckProofClass.BEHAVIOR),
        ("python -m pytest tests/x.py", EnumCheckProofClass.BEHAVIOR),
        # Not transparent, and none of these may start being so.
        (f'bash -c "{_PYTEST}"', EnumCheckProofClass.INDETERMINATE),
        (f"ssh host {_PYTEST}", EnumCheckProofClass.INDETERMINATE),
        ("aws ssm send-command --comment x", EnumCheckProofClass.INDETERMINATE),
        (
            "kubectl -n onex-dev exec pod -- onex --version",
            EnumCheckProofClass.INDETERMINATE,
        ),
        # OMN-18135 AC4 moved this one deliberately: `curl --fail` is an
        # ASSERTED live read, so it is now READBACK rather than nothing.
        # It still is not, and can never be, BEHAVIOR.
        ("curl -sf https://example.invalid/health", EnumCheckProofClass.READBACK),
        ("docker exec c psql -c 'select 1'", EnumCheckProofClass.INDETERMINATE),
        # Merge-state and static-inspection verdicts are untouched.
        # A bare `gh pr view` is OMN-15391's PR-state surrogate, checked
        # ahead of the head walk, so it never reaches the merge-state leg.
        (
            "gh pr view 3390 --repo OmniNode-ai/omnibase_infra --json files",
            EnumCheckProofClass.SURROGATE,
        ),
        # An ASSERTED merge probe does reach it.
        (
            "gh api repos/OmniNode-ai/omnibase_infra/commits/abc --jq .sha",
            EnumCheckProofClass.MERGE_STATE,
        ),
        ("grep -c 'def thing' src/x.py", EnumCheckProofClass.SURROGATE),
        (
            "cd omnibase_infra && grep -c 'def thing' src/x.py",
            EnumCheckProofClass.SURROGATE,
        ),
    ],
)
def test_controls_are_unmoved(command: str, expected: EnumCheckProofClass) -> None:
    """Everything that is not an `env` option or a `cd` classifies as before."""
    assert classify_command(command) is expected


def test_exit_code_laundering_still_wins_over_transparency() -> None:
    """A transparent prefix cannot rescue a command whose exit code is discarded.

    `cd x && uv run pytest ... || true` runs the runner and then throws its
    verdict away. Transparency is about the HEAD; the laundering guard is
    about the exit status, and it must still fire first.
    """
    assert (
        classify_command(f"cd omnibase_infra && {_PYTEST} || true")
        is EnumCheckProofClass.INDETERMINATE
    )
    assert (
        classify_command(f"env -u PYTHONPATH {_PYTEST} || true")
        is EnumCheckProofClass.INDETERMINATE
    )


def test_a_foreign_suite_stays_a_surrogate_behind_a_transparent_prefix() -> None:
    """OMN-15391's corpus is checked first and transparency must not bypass it.

    A denylisted generic suite is a real pytest run, so making `cd`
    transparent would call it BEHAVIOR if the surrogate check were reachable
    only through the head walk. It is not — `classify_command` delegates to
    the OMN-15391 predicate before any of this — and that ordering is pinned
    here.
    """
    foreign = "uv run pytest tests/test_evidence_admissibility.py -q"
    assert classify_command(foreign) is EnumCheckProofClass.SURROGATE
    assert classify_command(f"cd onex_change_control && {foreign}") is (
        EnumCheckProofClass.SURROGATE
    )
