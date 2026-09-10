# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18135 AC4 — an asserted live readback is its own proof class.

THE RULING THIS IMPLEMENTS, and its provenance
----------------------------------------------
Orchestrator ruling under the deterministic-truth doctrine, recorded at
``docs/tracking/ROLLING_WORK_LEDGER.md:6148`` (2026-09-10T17:50:03Z). Not an
operator ruling; stated precisely rather than upgraded:

    an asserted live or shell readback is admissible as its own proof class,
    readback, for a criterion whose text asserts live STATE (a condition, a
    row, a count, a config value read from the running system with a positive
    control); it never proves BEHAVIOUR (a criterion that asserts what code
    does), which stays test-runner/onex-CLI only.

This module implements the CLASS. The half that decides whether a criterion is
state-shaped, and therefore whether a readback may discharge it, cannot live
here: ``node_dod_verify`` has no Linear access and never sees acceptance
criteria. That join is in the closer.

WHAT THIS WIDENS, said plainly
------------------------------
This is the loosening direction on a gate whose whole declared asymmetry is
that a false hold costs a comment and a false flip writes an unearned Done.
Three things bound it:

1. **The class is asked LAST.** ``classify_command`` runs its existing walk
   first and only asks the readback question where it would otherwise return
   ``INDETERMINATE``. So this change can promote indeterminate to readback and
   can never touch a ``BEHAVIOR``, ``MERGE_STATE`` or ``SURROGATE`` verdict.
   Nothing that classifies today is demoted, and nothing is upgraded past its
   current strength.
2. **It never counts as behaviour.** ``behavior_proving_count`` is unchanged
   and a readback can never satisfy it. A separate
   ``readback_proving_count`` carries the new fact, so a consumer that has not
   been taught about readbacks sees exactly what it saw before.
3. **An unasserted read is still nothing.** The command's exit status must be
   able to go red on what it read.

THE HONEST LIMIT, stated rather than implied
--------------------------------------------
The ruling says "with a positive control". **A positive control is not
checkable from a command shape and this module does not check it.** What is
checkable is the ASSERTION — that the exit status depends on the value read —
and that is what is enforced. Whether the author also ran the negative case is
a judgement the contract's reviewer makes, exactly as it is for a behaviour
check. Claiming otherwise would be the kind of gate that reports green while
doing nothing.

WHY A HEAD WALK COULD NOT HAVE DONE THIS
----------------------------------------
Measured on the worked example the ruling names. OMN-17771's five items are
shell programs, not prefixed runners::

    tot=0; for f in a b c; do body="$(gh api ... --jq '.content' | base64 -d)";
    test -n "$body" || exit 1; n=$(printf '%s' "$body" | grep -cE '...');
    tot=$((tot+n)); done; test "$tot" = "0"

Split into pipeline segments, the heads are ``for``, ``test``, ``done`` and
assignments. No per-segment head verdict describes that program. The readback
question is therefore asked of the WHOLE text: does it read a live surface,
and can its exit status go red on what it read.
"""

from __future__ import annotations

import pytest

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.services.check_proof_class import (
    classify_command,
)

pytestmark = pytest.mark.unit

#: The worked example the ruling names, verbatim from contracts/OMN-17771.yaml
#: at OCC origin/dev. Reads a live surface through `gh api` and asserts a count
#: on what came back.
_OMN_17771_AC1_NEGATIVE = (
    "tot=0; for f in customer-getting-started customer-api-key-security "
    "customer-beta-gateway-and-delegation customer-usage-and-plans; do "
    'body="$(gh api "repos/OmniNode-ai/knowledge-base-internal/contents/'
    "guides/$f.md?ref=main\" --jq '.content' | base64 -d)\"; "
    'test -n "$body" || exit 1; '
    "n=$(printf '%s' \"$body\" | grep -cE 'client_id=(omnidash-spa|omniweb)'); "
    'tot=$((tot+n)); done; test "$tot" = "0"'
)

#: Its positive control: the same read, asserting the customer id IS present.
_OMN_17771_AC1_POSITIVE = (
    "tot=0; for f in customer-getting-started customer-api-key-security; do "
    'body="$(gh api "repos/OmniNode-ai/knowledge-base-internal/contents/'
    "guides/$f.md?ref=main\" --jq '.content' | base64 -d)\"; "
    'test -n "$body" || exit 1; '
    "n=$(printf '%s' \"$body\" | grep -c 'client_id=onex-customer'); "
    'tot=$((tot+n)); done; test "$tot" -gt "0"'
)


# --------------------------------------------------------------------------
# The class exists and the worked example reaches it.
# --------------------------------------------------------------------------


def test_the_enum_carries_the_class() -> None:
    """A distinct member, not an alias of an existing one.

    Pinned because the whole point of the ruling is that a readback is
    admissible AND is not behaviour. A value that collided with either would
    make the distinction unstatable.
    """
    assert EnumCheckProofClass.READBACK.value == "readback"
    assert EnumCheckProofClass.READBACK is not EnumCheckProofClass.BEHAVIOR
    assert EnumCheckProofClass.READBACK is not EnumCheckProofClass.INDETERMINATE


@pytest.mark.parametrize("command", [_OMN_17771_AC1_NEGATIVE, _OMN_17771_AC1_POSITIVE])
def test_the_omn_17771_shell_readbacks_are_admissible(command: str) -> None:
    """The worked example the ruling names, verbatim from its contract.

    Both halves of its control pair: one asserts a count of zero for the
    internal client ids, the other asserts a count above zero for the customer
    id. Each reads a live surface and each can go red on what it read.
    """
    assert classify_command(command) is EnumCheckProofClass.READBACK


@pytest.mark.parametrize(
    "command",
    [
        # A live HTTP read whose exit status follows the response status.
        "curl -sf https://dev.api.omninode.ai/health",
        # A live HTTP read asserted on its body.
        "curl -sS https://dev.auth.omninode.ai/realms/omninode | grep -q kc-register-form",
        # Cluster state, asserted.
        "kubectl -n onex-dev get deploy omninode-runtime -o jsonpath='{.spec.replicas}' | grep -q '^1$'",
        # A row count from the running database, asserted.
        "psql -tAc 'select count(*) from tenant_registry_mirror' | grep -q '^4$'",
        # A config value read over ssh, asserted.
        "ssh host \"docker inspect omninode-runtime\" | jq -e '.[0].State.Running'",
        # Broker state, asserted with test.
        'test "$(rpk topic list | grep -c onex.tenant.events)" = "1"',
    ],
)
def test_an_asserted_live_read_is_a_readback(command: str) -> None:
    """Reads a running system, and its exit status turns on what it read."""
    assert classify_command(command) is EnumCheckProofClass.READBACK


# --------------------------------------------------------------------------
# The bounds. Each of these is a way the class could have been too generous.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # No assertion: exits 0 whatever the cluster says.
        "kubectl -n onex-dev get deploy omninode-runtime -o yaml",
        "aws ssm send-command --comment x",
        "ssh host 'docker ps'",
        # `curl` WITHOUT --fail exits 0 on a 500.
        "curl -sS https://dev.api.omninode.ai/health",
        "docker exec c psql -c 'select 1'",
    ],
)
def test_an_unasserted_live_read_is_still_nothing(command: str) -> None:
    """A read whose exit status cannot depend on what it read proves nothing.

    This is the bound that keeps the class honest: `readback` is the ASSERTED
    form. A bare read is green whatever the system says, which is the same
    vacuity OMN-15391 refuses elsewhere.
    """
    assert classify_command(command) is EnumCheckProofClass.INDETERMINATE


def test_a_readback_never_becomes_behaviour() -> None:
    """The ruling's hard line, asserted rather than trusted."""
    for command in (
        _OMN_17771_AC1_NEGATIVE,
        _OMN_17771_AC1_POSITIVE,
        "curl -sf https://dev.api.omninode.ai/health",
    ):
        assert classify_command(command) is not EnumCheckProofClass.BEHAVIOR


def test_exit_code_laundering_still_wins_over_a_readback() -> None:
    """A discarded exit code is not an assertion, however live the read."""
    assert (
        classify_command("curl -sf https://dev.api.omninode.ai/health || true")
        is EnumCheckProofClass.INDETERMINATE
    )


# --------------------------------------------------------------------------
# Monotonicity: the class is asked LAST, so nothing that classifies today
# can move. This is the property that makes the widening bounded.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # A runner after a RECOGNISED segment is still behaviour.
        (
            "gh api repos/OmniNode-ai/omnibase_infra/commits/abc --jq .sha "
            "&& uv run pytest tests/x.py -q",
            EnumCheckProofClass.BEHAVIOR,
        ),
        # RESIDUAL, asserted rather than hidden. A runner after an
        # UNRECOGNISED segment is not reached: the walk returns on the first
        # head it does not know, which is the same early exit `cd` exposed.
        # This change does not fix that -- it moves this case from
        # INDETERMINATE to READBACK, which is still a promotion and not a
        # demotion. Widening the walk itself is a separate call with its own
        # loosening argument, and is deliberately not made here.
        (
            "curl -sf https://dev.api.omninode.ai/health && uv run pytest tests/x.py -q",
            EnumCheckProofClass.READBACK,
        ),
        ("uv run pytest tests/x.py -q", EnumCheckProofClass.BEHAVIOR),
        ("onex run-node node_x --input y", EnumCheckProofClass.BEHAVIOR),
        # Merge state is still merge state, asserted or not.
        (
            "gh api repos/OmniNode-ai/omnibase_infra/commits/abc --jq .sha",
            EnumCheckProofClass.MERGE_STATE,
        ),
        # OMN-15391's corpus is still checked ahead of everything.
        (
            "gh pr view 3390 --repo OmniNode-ai/omnibase_infra --json files",
            EnumCheckProofClass.SURROGATE,
        ),
        (
            "uv run pytest tests/test_evidence_admissibility.py -q",
            EnumCheckProofClass.SURROGATE,
        ),
        # Static inspection of the tree is not a readback: nothing live is read.
        ("grep -c 'def thing' src/x.py", EnumCheckProofClass.SURROGATE),
        (
            "test \"$(grep -c 'def thing' src/x.py)\" = '1'",
            EnumCheckProofClass.SURROGATE,
        ),
        # OMN-18135's transparent prefixes are unaffected.
        (
            "cd omnibase_infra && uv run pytest tests/x.py -q",
            EnumCheckProofClass.BEHAVIOR,
        ),
        (
            "env -u PYTHONPATH uv run pytest tests/x.py -q",
            EnumCheckProofClass.BEHAVIOR,
        ),
    ],
)
def test_nothing_that_classifies_today_moves(
    command: str, expected: EnumCheckProofClass
) -> None:
    """Every verdict the walk already reaches is untouched by the new class."""
    assert classify_command(command) is expected


# --------------------------------------------------------------------------
# The count the closer will read. Separate from behaviour, never folded in.
# --------------------------------------------------------------------------


def test_readback_is_counted_separately_from_behaviour() -> None:
    """`readback_proving_count` carries the new fact; behaviour is untouched.

    Kept apart deliberately. A readback proves what the system currently IS
    and never what the code DOES, so folding it into `behavior_proving_count`
    would let a readback satisfy a conjunct the ruling says it cannot. It
    also means a consumer that has not been taught about readbacks reads
    exactly the number it read before.
    """
    from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
        ModelDodVerifyState,
    )

    fields = ModelDodVerifyState.model_fields
    assert "readback_proving_count" in fields
    assert "behavior_proving_count" in fields
    assert fields["readback_proving_count"].default == 0
