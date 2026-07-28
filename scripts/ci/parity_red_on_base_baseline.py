# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""FROZEN parity-RED-on-base debt baseline — OMN-15344 (child of OMN-14355).

``PARITY_RED_ON_BASE_DEBT`` is the frozen set of nodes whose committed hand-flip proof
declares parity tests that are NOT red-on-their-own-assertion at the receipt's own
``base_ref``. Those tests run green on HEAD and prove nothing about the def-A -> def-B
behavior transfer, because they never discriminated it.

Measured, not assumed. On 2026-07-28 assertion 7 of
``omnibase_core/scripts/ci/verify_flip_bundle.py`` (OMN-15340, merged omnibase_core#1519)
was EXECUTED against every committed ``*.handflip.json`` in this repo with the
newness/grandfathering rule bypassed. Every id below FAILED.
6 of this repo's 7 committed hand-flip receipts are listed. The one absentee, ``omnimarket.nodes.node_occ_companion_compute``, genuinely PASSES assertion 7 and is the only receipt in either repo that does.
Across omnibase_infra + omnimarket that census read 21/22 receipts failing and 102/126
declared parity ids not red-for-the-right-reason.

This set is monotonically NON-INCREASING. It may only shrink, and the gate that reads it
(``verify_flip_bundle.py``, wired into this repo's CI in the same job as the
canonical-shape ratchet) enforces that:

* A NEW hand-flip may NOT be added here. It must declare parity tests that are RED on
  their own assertion at its ``base_ref``. Growth HARD-FAILS.
* An entry naming a hand-flip proof that does not exist HARD-FAILS, so this list cannot
  rot into decoration and cannot be pre-seeded ahead of a flip.
* REMOVING an entry is a CLAIM that the node now passes, and the gate EXECUTES it:
  assertion 7 is re-armed for that node and must pass. Removing an entry by deleting the
  hand-flip proof instead HARD-FAILS too.

``RED_EXCEPTION`` is inadmissible and stays that way. A base-tree exception is
indistinguishable from a test that is merely incompatible with the old code, so it is not
a discriminating claim — and it is the single largest slice of this census, which is
exactly why admitting it would erase most of this list without one test getting better.

Retirement mechanism: burn down per node as each is next touched — rewrite its declared
parity tests so they assert the transition, prove them RED at the receipt's ``base_ref``,
then delete the entry in the same PR.
"""

PARITY_RED_ON_BASE_DEBT: tuple[str, ...] = (
    "omnimarket.nodes.node_llm_delegation_routing_compute",
    "omnimarket.nodes.node_persona_builder_compute",
    "omnimarket.nodes.node_pr_lifecycle_triage_compute",
    "omnimarket.nodes.node_prod_promotion_gate_compute",
    "omnimarket.nodes.node_rsd_fill_compute",
    "omnimarket.nodes.node_similarity_compute",
)
