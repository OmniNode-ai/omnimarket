# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-check proof class for DoD evidence verdicts (OMN-15911).

What a single executed evidence check actually BINDS. This is the axis
``node_dod_verify`` verdicts were missing: before OMN-15911 a check that read
a PR's merge state and a check that executed the ticket's tests both
terminated in the identical ``status: verified``, so "N/N verified" could not
be read as "the behavior works" — only as "N checks exited 0".

Relationship to the other two enums that share the name
-------------------------------------------------------
There are three, and they answer three different questions. Do not merge them
and do not import one in place of another:

* ``omnibase_core.enums.enum_proof_class.EnumProofClass`` — the OMN-13977
  doctrine vocabulary (``code-only | receipt-bound | deployed | live-readback
  | replay-proven | prod-proven``), landed on ``ModelTicketContract`` by
  omnibase_core#1559. It is a **contract-level** claim: which surface proves
  this TICKET. It is deliberately NOT reused here, because it cannot answer
  the per-check question without lying: by its own definition ``live-readback``
  is "observed on a live surface (gh/Linear/ssh/projection)", which a bare
  ``gh pr view --json state`` satisfies — the exact merge-state-only check
  this enum exists to separate out. One taxonomy, two questions, would make
  the discriminator unable to discriminate.
* ``omnimarket.enums.enum_proof_class.EnumProofClass`` — LLM evidence-bundle
  token-count provenance for on-vs-off experiments (OMN-12794). Unrelated.
* This enum — per-check, verdict-level, derived from the executed command.

Derived from the command, never from the contract's prose
---------------------------------------------------------
Classification reads the ``check_value``/``command`` that actually runs. It
never trusts ``check_type`` or ``description``, because OMN-15391 measured
exactly that gap: an evidence item's prose and its command are allowed to
describe different proofs and no gate compares them.
"""

from __future__ import annotations

from enum import StrEnum


class EnumCheckProofClass(StrEnum):
    """What one DoD evidence check binds when it passes.

    Ordering note: :data:`CHECK_PROOF_CLASS_PRECEDENCE` in
    ``omnimarket.nodes.node_dod_verify.services.check_proof_class`` defines the
    roll-up order for a multi-check evidence item. Enum member order here is
    documentation only.
    """

    # Executes the claimed behavior — a test runner, or the product's own CLI.
    # The only class that can release an automated Done flip.
    BEHAVIOR = "behavior"

    # Binds PR / merge / repo state: the change landed. Proves a merge
    # happened; proves nothing about whether the merged code does anything.
    MERGE_STATE = "merge-state"

    # A stand-in. Either static-artifact inspection (a file exists, a string
    # is present) or a generic, ticket-independent suite standing in for a
    # ticket-specific proof (the OMN-15391 class). Executed and falsifiable,
    # and still not evidence for THIS ticket.
    SURROGATE = "surrogate"

    # The command shape could not be classified. Fails closed: never counted
    # as behavior-proving, so an unrecognized shape holds a flip instead of
    # releasing one.
    INDETERMINATE = "indeterminate"


__all__ = ["EnumCheckProofClass"]
