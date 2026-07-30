# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15390 — the runner must read ``supersedes_dod_evidence`` the way the gate does.

Two tools consume ``dod_evidence[]``. Before this ticket only ONE honoured the
append-only contract-repair idiom
(``evidence_artifact: "supersedes_dod_evidence:<id>"``):

* ``onex_change_control .../contract_compliance_check.py::_superseded_dod_ids``
  marked the retired id ``superseded`` and emitted WARN;
* ``node_dod_verify``'s ``EvidenceCollector`` had zero handling of it.

Proven by execution 2026-07-29T16:45Z: an ``OMN-14968`` ``dod_verify`` re-run
returned FAIL 16/22 in which every ``-rebind``/``-strict`` superseding check
PASSED while its superseded original still ran and still failed. Because
``dod_verify`` is the only sanctioned Done-flip path, an append-only repair
could silence the gate but could never close the ticket.

The supersession mechanism itself lands in omnimarket#1951 (OMN-15382); this
module is the OMN-15390 delta on top of it and covers the three things that
mechanism did not:

1. **Ordering parity (ticket AC1).** ``_superseded_dod_ids`` supersedes a target
   only ``if supersedes in seen`` — ONLY a later item may retire an earlier one.
   Resolving against the whole-contract id set instead lets a FORWARD marker
   retire the newest entry in the runner while the gate still executes it: a
   runner-more-permissive-than-gate divergence. ``TestOrderingParityWithTheGate``
   pins the rule; ``test_runner_matches_the_gate_algorithm_over_every_small_contract``
   proves set-equality exhaustively rather than by review inference.
2. **Verdict-bearing denominator (ticket AC4).** ``total_checks`` excludes
   superseded entries so a fully-repaired contract reads N/N.
3. **Anti-laundering (ticket AC2, read strictly).** Supersession may remove a
   FALSE red; it may never manufacture a green. Asserted at the RECEIPT layer —
   the durable artifact — not just at the inner state.

Adversarial-review remediation (2026-07-29, after omnimarket#1952 merged into
its stacked parent). Two of the above were asserted but did not hold, and the
tests that named them were built so they could not notice:

* **The anti-laundering guard was scoped to a GLOBAL ``verified == 0``**, which
  any single unrelated passing sibling defeats. Executed against the real
  collector/handler/``_build_receipt`` path, the contract ``[dod-fail(false),
  dod-other(true), dod-marker(checks: [], supersedes dod-fail)]`` receipted
  **PASS** while the identical contract without the marker receipted **FAIL** —
  appending one evidence-free item still laundered a red into a green. Only the
  degenerate zero-passing-item case had been closed, and the test named for the
  property exercised only that case. The rule is now PER-EDGE
  (``EvidenceCollector._supersession_is_in_effect``): an edge fires only when
  the item that ultimately carries the verdict is itself VERIFIED.
* **The "identical for every input" parity claim was false.** The runner skipped
  a marker-carrying item BEFORE reading its marker whenever the carrier's own
  ``id`` was missing/empty/non-string/null, while OCC's real
  ``_superseded_dod_ids`` evaluates ``supersedes in seen`` regardless — 4 of 5
  such shapes diverged, in the runner-STRICTER-than-gate direction (this
  ticket's original bug class), and with an EMPTY ``malformed`` map, i.e.
  silently. The exhaustive differential could not catch it because its domain
  emitted only well-formed string ids. The domain now includes those shapes
  (:func:`_non_canonical_id_contracts`), and
  :func:`test_the_exhaustive_domain_actually_contains_the_divergence_it_claims`
  stops it being narrowed back.

RED-before / GREEN-after (recorded, both directions):

* Reverting the ordering rule in ``_resolve_supersessions`` to the position-blind
  whole-contract lookup turns ``TestOrderingParityWithTheGate`` and the
  exhaustive differential RED (the forward-marker cases diverge from the gate).
* Reverting ``total_checks=non_superseded_total`` to ``len(checks)`` turns
  ``test_total_checks_is_the_verdict_bearing_denominator`` RED.
* Reverting ``_supersession_is_in_effect`` to "any well-formed edge fires" turns
  ``test_marker_that_proves_nothing_cannot_launder_a_fail_into_a_pass_receipt``,
  ``test_a_superseder_that_does_not_verify_retires_nothing`` and
  ``test_a_chain_is_carried_by_its_terminal_item`` RED.
* Restoring the ``if not isinstance(item_id, str) or not item_id: continue``
  guard ahead of the marker read turns
  ``test_runner_matches_the_gate_algorithm_over_every_small_contract``,
  ``test_a_marker_on_an_id_less_item_is_honoured_exactly_like_the_gate`` and
  ``test_a_broken_marker_on_an_id_less_item_is_not_a_silent_skip`` RED.

Second adversarial-review round (2026-07-29, after omnimarket#1957 merged into
``dev`` at ``ddb7228d``). Three residuals, all of which were places where the
suite's own shape was hiding the defect:

* **R1 — the self-reference test ran BEFORE the ordering test.** The gate has
  no self-reference branch at all; it asks only ``if supersedes in seen``, and
  ``seen.add`` runs after. A self-reference is inert only *because* of that
  ordering — so when an EARLIER item declares the same id, ``seen`` DOES hold
  it and the gate retires that earlier entry, while the runner hard-REDded the
  carrier. Runner-STRICTER-than-gate, on 144 of the 544 contracts in
  :func:`_duplicate_id_contracts`, which the domain did not contain because
  every previous generator emitted unique ids. Now 0 of 544, asserted against
  both the transcribed oracle and the real OCC function.
* **R2 — the domain-wide invariant test truncated at ``[:20]``**, one contract
  short of the ``id: 7`` / ``id: None`` carriers. Those did not merely fail;
  they raised ``ValidationError`` out of ``collect()`` and produced NO RECEIPT
  AT ALL. The truncation is what kept the suite green. The full domain now
  runs, and every unrepresentable shape fails CLOSED with a receipt
  (:class:`TestAContractTheReceiptCannotRepresentFailsClosed`).
* **R3 — the real-OCC differential skipped in hosted CI**, so the only
  cross-repo parity proof in the repo never executed on a PR. ``ci.yml``'s
  ``test`` job now checks OCC out (public repo, default token, existing
  pattern), and
  :func:`test_ci_wires_an_occ_checkout_into_the_job_that_runs_this_suite`
  keeps the step there.

Third adversarial-review round (2026-07-29, after omnimarket#1959 merged into
``dev`` at ``b0558db7``). One finding, and it is the same failure mode as R2 —
a claim in the source that the suite did not check:

* **R1's reorder made two comments false, and neither was updated.**
  ``_terminal_superseder``'s docstring said the walk "is acyclic by
  construction and terminates; the visited-set guard is a defensive backstop,
  not a live path", and the module-level comment in ``evidence_collector.py``
  said "the relation is acyclic by construction … there is no cycle to
  detect". Both stopped being true the moment ``target in seen`` was tested
  ahead of ``target == item_id_str``: a self-referential marker on a duplicate
  id is now an ACCEPTED edge, and because ``superseded`` is keyed by ID while
  the backwards-ordering property holds by INDEX, that edge is an id-level
  SELF-LOOP (``superseded['dod-0'] == 1`` with ``id_at[1] == 'dod-0'``).
  Measured over this module's own domains, the guard fires on **176 of 296
  edges (59.5%)** of :func:`_duplicate_id_contracts` versus **0 of 75** on the
  unique-id :func:`_small_contracts` — dead only on the domain the claim was
  written against. Removing the guard does not degrade gracefully: the walk
  never terminates, and since ``_collect_impl`` phase 2 calls it, ``collect()``
  itself hangs (verified — the end-to-end self-loop case did not return in 5
  minutes with the guard deleted). Shipped BEHAVIOUR was already correct and
  fail-safe; the defect was a false load-bearing claim mislabelling live code
  as dead. :class:`TestTheVisitedSetGuardIsRequiredNotDefensive` pins it, and
  is deliberately declared AHEAD of the expensive domain-wide test so guard
  removal surfaces as a fast bounded-subprocess failure instead of wedging the
  whole suite.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.__main__ import _build_receipt
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
    _supersedes_marker,
)

_CORPUS_PATH = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "dod_supersession"
    / "parity_corpus.yaml"
)

_REPO_ROOT = Path(__file__).resolve().parents[4]

_IN_WORKSPACE_OCC = _REPO_ROOT / "onex_change_control"
"""Where ``ci.yml``'s ``test`` job checks onex_change_control out (OMN-15390 R3).

A repo-root-relative path rather than an env var on purpose: exporting
``ONEX_CC_REPO_PATH`` across the test job would re-point
``EvidenceCollector._resolve_contract_repo_dir`` for every other test in the
suite, which is exactly the hermeticity trap three OMN-15382 tests already hit.
"""

_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"

_MARKER_PREFIX = "supersedes_dod_evidence:"


# ---------------------------------------------------------------------------
# Helpers — these drive the REAL collector and the REAL handler, never a
# surrogate. ``_verify`` is the same path ``onex skill dod_verify`` takes.
# ---------------------------------------------------------------------------


_NO_ID = object()
"""Sentinel for ``_item(..., item_id=_NO_ID)`` — emit an item with no ``id`` key."""


def _item(
    item_id: Any,
    *,
    check_value: str | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    """Build one dod_evidence item.

    ``item_id`` is deliberately typed ``Any``: the OCC gate honours a marker
    regardless of whether the CARRYING item has a usable ``id``, so the parity
    tests must be able to express a carrier with no ``id`` key (``_NO_ID``), an
    empty string, a non-string, or ``None``.
    """
    item: dict[str, Any] = {"description": f"item {item_id}"}
    if item_id is not _NO_ID:
        item["id"] = item_id
    item["checks"] = (
        []
        if check_value is None
        else [{"check_type": "command", "check_value": check_value}]
    )
    if supersedes is not None:
        item["evidence_artifact"] = f"{_MARKER_PREFIX}{supersedes}"
    return item


def _write_contract(
    tmp_path: Path, dod_evidence: list[dict[str, Any]]
) -> tuple[str, Path]:
    ticket_id = f"OMN-{uuid4().int % 90000 + 10000}"
    contract_path = tmp_path / f"{ticket_id}.yaml"
    contract_path.write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": ticket_id,
                "dod_evidence": dod_evidence,
            }
        ),
        encoding="utf-8",
    )
    return ticket_id, contract_path


def _verify(tmp_path: Path, dod_evidence: list[dict[str, Any]]) -> ModelDodVerifyState:
    """Load + execute a real contract through the real collector and handler."""
    ticket_id, contract_path = _write_contract(tmp_path, dod_evidence)
    results = EvidenceCollector().collect(ticket_id, contract_path=str(contract_path))
    state = HandlerDodVerify()._handle_typed(
        ModelDodVerifyStartCommand(
            ticket_id=ticket_id,
            contract_path=str(contract_path),
        ),
        evidence_results=results,
    )
    return state


def _by_id(state: ModelDodVerifyState) -> dict[str, EnumEvidenceCheckStatus]:
    return {check.evidence_id: check.status for check in state.checks}


def _resolve(dod_evidence: list[Any]) -> tuple[set[str], set[str]]:
    """(superseded ids, LABELS of items carrying a marker that resolved to nothing).

    ``_SupersessionResolution.malformed`` is keyed by item INDEX so a broken
    marker on an item with a missing/empty/non-string ``id`` is still
    reportable instead of being a silent no-op. Tests and the shared corpus
    stay id-expressed, so this translates each index back to the carrier's
    label — its ``id`` when it has a usable one, else ``dod_evidence[<n>]``,
    which is exactly what the runner puts in the receipt.
    """
    resolution = EvidenceCollector._resolve_supersessions(dod_evidence)
    labels: set[str] = set()
    for index in resolution.malformed:
        item = dod_evidence[index]
        item_id = item.get("id") if isinstance(item, dict) else None
        labels.add(
            item_id
            if isinstance(item_id, str) and item_id
            else f"dod_evidence[{index}]"
        )
    return set(resolution.superseded), labels


# ---------------------------------------------------------------------------
# The defect this ticket was filed for.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSupersessionRemovesAFalseRed:
    def test_superseded_original_is_retired_and_the_repair_carries_the_verdict(
        self, tmp_path: Path
    ) -> None:
        """The 16:45Z OMN-14968 shape, minimised: bad original + good repair."""
        state = _verify(
            tmp_path,
            [
                _item("dod-orig", check_value="false"),
                _item("dod-orig-rebind", check_value="true", supersedes="dod-orig"),
            ],
        )

        statuses = _by_id(state)
        assert statuses["dod-orig"] == EnumEvidenceCheckStatus.SUPERSEDED
        assert statuses["dod-orig-rebind"] == EnumEvidenceCheckStatus.VERIFIED
        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert state.failed_count == 0
        assert state.superseded_count == 1

    def test_superseded_entry_stays_visible_in_the_receipt(
        self, tmp_path: Path
    ) -> None:
        """Retired, not deleted — the receipt must show the repair happened."""
        state = _verify(
            tmp_path,
            [
                _item("dod-orig", check_value="false"),
                _item("dod-orig-rebind", check_value="true", supersedes="dod-orig"),
            ],
        )
        receipt = _build_receipt(state, None, tmp_path)
        probe_stdout = str(receipt["probe_stdout"])

        assert '"superseded": 1' in probe_stdout
        assert "dod-orig" in probe_stdout
        assert "superseded" in probe_stdout.lower()

    def test_total_checks_is_the_verdict_bearing_denominator(
        self, tmp_path: Path
    ) -> None:
        """AC4: a fully-repaired contract reads N/N, not N/(N+superseded)."""
        state = _verify(
            tmp_path,
            [
                _item("dod-orig-a", check_value="false"),
                _item("dod-orig-b", check_value="false"),
                _item("dod-a-rebind", check_value="true", supersedes="dod-orig-a"),
                _item("dod-b-rebind", check_value="true", supersedes="dod-orig-b"),
            ],
        )
        assert state.superseded_count == 2
        assert state.verified_count == 2
        assert state.total_checks == 2
        assert state.verified_count == state.total_checks


@pytest.mark.unit
class TestSupersessionCannotRescueAnUnrelatedFailure:
    def test_a_failing_sibling_still_fails_the_contract(self, tmp_path: Path) -> None:
        """(d) Supersession removes ITS OWN false red and nothing else."""
        state = _verify(
            tmp_path,
            [
                _item("dod-orig", check_value="false"),
                _item("dod-unrelated", check_value="false"),
                _item("dod-orig-rebind", check_value="true", supersedes="dod-orig"),
            ],
        )

        statuses = _by_id(state)
        assert statuses["dod-orig"] == EnumEvidenceCheckStatus.SUPERSEDED
        assert statuses["dod-unrelated"] == EnumEvidenceCheckStatus.FAILED
        assert state.status == EnumDodVerifyStatus.FAILED
        assert state.failed_count == 1
        assert _build_receipt(state, None, tmp_path)["status"] == "FAIL"

    def test_marker_that_proves_nothing_cannot_launder_a_fail_into_a_pass_receipt(
        self, tmp_path: Path
    ) -> None:
        """Anti-laundering, asserted at the RECEIPT — the durable artifact.

        Appending ONE marker item that declares no passing check of its own
        must not retire a genuinely-failing entry. Supersession may remove a
        FALSE red; it may never manufacture a green.

        The contract carries a PASSING sibling on purpose. An earlier revision
        of this fix guarded only on a GLOBAL ``verified == 0``, which the
        single-item shape below satisfies but which ANY unrelated passing
        entry defeats — so that guard closed only the degenerate case while
        the property this test names still did not hold. The real rule is
        per-edge (``_supersession_is_in_effect``): the SUPERSEDING item must
        verify in its own right.
        """
        without_marker = _verify(
            tmp_path,
            [
                _item("dod-a", check_value="false"),
                _item("dod-pass", check_value="true"),
            ],
        )
        assert without_marker.status == EnumDodVerifyStatus.FAILED
        assert _build_receipt(without_marker, None, tmp_path)["status"] == "FAIL"

        with_marker = _verify(
            tmp_path,
            [
                _item("dod-a", check_value="false"),
                _item("dod-pass", check_value="true"),
                _item("dod-marker", supersedes="dod-a"),
            ],
        )
        statuses = _by_id(with_marker)

        # The marker proved nothing (``checks: []`` -> SKIPPED), so it retired
        # nothing and the failing entry was executed normally.
        assert statuses["dod-marker"] == EnumEvidenceCheckStatus.SKIPPED
        assert statuses["dod-a"] == EnumEvidenceCheckStatus.FAILED
        assert with_marker.superseded_count == 0
        assert with_marker.failed_count == 1
        assert with_marker.status == EnumDodVerifyStatus.FAILED
        assert _build_receipt(with_marker, None, tmp_path)["status"] == "FAIL"

        # The rejection is stated on the entry, not inferred from a bare red.
        rejected = next(c for c in with_marker.checks if c.evidence_id == "dod-a")
        assert "SUPERSESSION_NOT_IN_EFFECT" in rejected.message

    @pytest.mark.parametrize(
        ("superseder_check", "expected_superseder_status"),
        [
            (None, EnumEvidenceCheckStatus.SKIPPED),  # checks: []
            ("false", EnumEvidenceCheckStatus.FAILED),  # its own check fails
        ],
        ids=["superseder_declares_no_checks", "superseder_own_check_fails"],
    )
    def test_a_superseder_that_does_not_verify_retires_nothing(
        self,
        tmp_path: Path,
        superseder_check: str | None,
        expected_superseder_status: EnumEvidenceCheckStatus,
    ) -> None:
        """A well-formed marker is NOT sufficient — the superseder must prove itself.

        Resolution says which edges are legal; effectiveness says which fire.
        A superseder that skips or fails retires nothing, so its target is
        executed and its own verdict stands.
        """
        state = _verify(
            tmp_path,
            [
                _item("dod-orig", check_value="false"),
                _item("dod-pass", check_value="true"),
                _item(
                    "dod-repair", check_value=superseder_check, supersedes="dod-orig"
                ),
            ],
        )
        statuses = _by_id(state)

        assert statuses["dod-repair"] == expected_superseder_status
        assert statuses["dod-orig"] == EnumEvidenceCheckStatus.FAILED
        assert state.superseded_count == 0
        assert state.status == EnumDodVerifyStatus.FAILED
        assert _build_receipt(state, None, tmp_path)["status"] == "FAIL"

    def test_a_verifying_superseder_still_retires_its_target(
        self, tmp_path: Path
    ) -> None:
        """The positive control for the rule above — the repair idiom still works.

        Without this, "make the superseder prove itself" could be satisfied by
        never superseding anything, which would re-break the ticket.
        """
        state = _verify(
            tmp_path,
            [
                _item("dod-orig", check_value="false"),
                _item("dod-pass", check_value="true"),
                _item("dod-repair", check_value="true", supersedes="dod-orig"),
            ],
        )
        statuses = _by_id(state)

        assert statuses["dod-orig"] == EnumEvidenceCheckStatus.SUPERSEDED
        assert statuses["dod-repair"] == EnumEvidenceCheckStatus.VERIFIED
        assert state.superseded_count == 1
        assert state.failed_count == 0
        assert state.status == EnumDodVerifyStatus.VERIFIED
        assert _build_receipt(state, None, tmp_path)["status"] == "PASS"

    def test_a_chain_is_carried_by_its_terminal_item(self, tmp_path: Path) -> None:
        """A retires-B-retires-C chain hangs on the TERMINAL item's verdict.

        The gate retires both ``dod-a`` and ``dod-b``, so ``dod-c`` is what
        actually proves anything. If ``dod-c`` proves nothing, neither earlier
        entry may be retired on its behalf.
        """
        proven = _verify(
            tmp_path,
            [
                _item("dod-a", check_value="false"),
                _item("dod-b", check_value="false", supersedes="dod-a"),
                _item("dod-c", check_value="true", supersedes="dod-b"),
            ],
        )
        assert _by_id(proven) == {
            "dod-a": EnumEvidenceCheckStatus.SUPERSEDED,
            "dod-b": EnumEvidenceCheckStatus.SUPERSEDED,
            "dod-c": EnumEvidenceCheckStatus.VERIFIED,
        }
        assert proven.status == EnumDodVerifyStatus.VERIFIED

        unproven = _verify(
            tmp_path,
            [
                _item("dod-a", check_value="false"),
                _item("dod-b", check_value="false", supersedes="dod-a"),
                _item("dod-c", supersedes="dod-b"),  # checks: [] -> proves nothing
            ],
        )
        statuses = _by_id(unproven)
        assert statuses["dod-a"] == EnumEvidenceCheckStatus.FAILED
        assert statuses["dod-b"] == EnumEvidenceCheckStatus.FAILED
        assert unproven.superseded_count == 0
        assert unproven.status == EnumDodVerifyStatus.FAILED
        assert _build_receipt(unproven, None, tmp_path)["status"] == "FAIL"


# ---------------------------------------------------------------------------
# Ordering parity — the half omnimarket#1951 did not carry.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOrderingParityWithTheGate:
    def test_forward_marker_retires_nothing_and_is_reported(
        self, tmp_path: Path
    ) -> None:
        """Only a LATER item may retire an EARLIER one (``if supersedes in seen``).

        A forward marker must not retire the later entry — that entry is the
        newest, most-correct proof in the contract, and the gate still executes
        it. Silently honouring it would make the runner accept a contract the
        gate rejects.
        """
        state = _verify(
            tmp_path,
            [
                _item("dod-early", check_value="true", supersedes="dod-late"),
                _item("dod-late", check_value="true"),
            ],
        )
        statuses = _by_id(state)

        assert statuses["dod-late"] != EnumEvidenceCheckStatus.SUPERSEDED
        assert statuses["dod-late"] == EnumEvidenceCheckStatus.VERIFIED
        assert statuses["dod-early"] == EnumEvidenceCheckStatus.FAILED
        assert state.status == EnumDodVerifyStatus.FAILED

    def test_dangling_marker_is_an_error_not_a_silent_skip(
        self, tmp_path: Path
    ) -> None:
        state = _verify(
            tmp_path,
            [
                _item("dod-a", check_value="true"),
                _item("dod-b", check_value="true", supersedes="dod-typo"),
            ],
        )
        statuses = _by_id(state)

        assert statuses["dod-b"] == EnumEvidenceCheckStatus.FAILED
        assert statuses["dod-a"] == EnumEvidenceCheckStatus.VERIFIED
        assert state.status == EnumDodVerifyStatus.FAILED

    def test_self_reference_is_an_error(self, tmp_path: Path) -> None:
        state = _verify(
            tmp_path,
            [_item("dod-a", check_value="true", supersedes="dod-a")],
        )
        assert _by_id(state)["dod-a"] == EnumEvidenceCheckStatus.FAILED
        assert state.status == EnumDodVerifyStatus.FAILED

    def test_a_broken_marker_produces_exactly_one_result_for_its_item(
        self, tmp_path: Path
    ) -> None:
        """No synthetic duplicate: one id -> one verdict-bearing result.

        A diagnostic emitted ALONGSIDE the item's normal execution would give
        the receipt two ``details`` entries with the same ``id`` and opposite
        statuses, so any consumer keying by id silently reads whichever it saw
        last. The malformed item is reported INSTEAD of executing, never as
        well as.
        """
        state = _verify(
            tmp_path,
            [
                _item("dod-a", check_value="true"),
                _item("dod-b", check_value="true", supersedes="dod-typo"),
            ],
        )
        ids = [check.evidence_id for check in state.checks]

        assert ids.count("dod-b") == 1
        assert len(ids) == len(set(ids))
        assert state.total_checks == len(state.checks)

    def test_attempted_cycle_terminates_deterministically(self, tmp_path: Path) -> None:
        """Ordering makes a cycle unrepresentable rather than merely detected."""
        superseded, malformed = _resolve(
            [
                _item("dod-a", check_value="true", supersedes="dod-b"),
                _item("dod-b", check_value="true", supersedes="dod-a"),
            ]
        )
        assert superseded == {"dod-a"}
        assert malformed == {"dod-a"}


# ---------------------------------------------------------------------------
# Parity corpus — the ONE shared definition of the marker semantics (AC5).
# ---------------------------------------------------------------------------


def _corpus_cases() -> list[dict[str, Any]]:
    raw = yaml.safe_load(_CORPUS_PATH.read_text(encoding="utf-8"))
    cases = raw["cases"]
    assert isinstance(cases, list)
    assert cases
    return list(cases)


@pytest.mark.unit
@pytest.mark.parametrize("case", _corpus_cases(), ids=lambda c: str(c["name"]))
def test_runner_matches_the_parity_corpus(case: dict[str, Any]) -> None:
    """The runner matches the shared corpus case-for-case.

    ``expected_superseded`` is the set BOTH consumers must agree on;
    ``expected_errors`` is the runner's additional fail-closed reporting, which
    never changes that set.
    """
    superseded, malformed = _resolve(case["dod_evidence"])
    assert superseded == set(case["expected_superseded"])
    assert malformed == set(case["expected_errors"])


@pytest.mark.unit
def test_corpus_keeps_its_discriminating_cases() -> None:
    """The corpus is not allowed to quietly lose the cases that make it bite."""
    cases = _corpus_cases()
    assert any(c["expected_superseded"] for c in cases)
    assert any(c["expected_errors"] for c in cases)
    assert any(not c["expected_superseded"] and not c["expected_errors"] for c in cases)


def _gate_algorithm_superseded_ids(dod_evidence: list[Any]) -> set[str]:
    """Literal transcription of ``contract_compliance_check._superseded_dod_ids``.

    This is a TEST ORACLE, not a second definition of the idiom: it exists so
    the equivalence below can be checked exhaustively in hosted CI, which has
    no ``onex_change_control`` checkout (omnimarket does not depend on OCC and
    must not grow that dependency). The transcription itself is pinned to the
    real function by ``test_occ_gate_agrees_with_the_runner_on_the_shared_corpus``
    whenever a checkout IS present, so drift in either direction is caught.
    """
    seen: set[str] = set()
    superseded: set[str] = set()
    for dod_item in dod_evidence:
        if not isinstance(dod_item, dict):
            continue
        item_id = dod_item.get("id")
        supersedes = _supersedes_marker(dod_item.get("evidence_artifact"))
        if supersedes in seen:
            superseded.add(supersedes)
        if isinstance(item_id, str):
            seen.add(item_id)
    return superseded


_NON_CANONICAL_IDS: list[Any] = [_NO_ID, "", 7, None]
"""Carrying-item ``id`` shapes the gate tolerates and the runner once did not.

``_superseded_dod_ids`` evaluates ``if supersedes in seen`` BEFORE (and
independently of) anything about the carrier's own ``id``. A revision of this
runner gated on the carrier's id first and so silently missed the supersession
for every one of these four shapes — runner-STRICTER-than-gate, this ticket's
original bug class re-created. The exhaustive differential below is only
non-vacuous on that axis if the domain actually contains them.
"""


def _small_contracts() -> list[list[dict[str, Any]]]:
    """Every contract of 1..3 items over a fixed id alphabet and marker space."""
    ids = ["dod-0", "dod-1", "dod-2"]
    marker_choices: list[str | None] = [None, *ids, "dod-absent"]
    contracts: list[list[dict[str, Any]]] = []
    for length in (1, 2, 3):
        for markers in itertools.product(marker_choices, repeat=length):
            contracts.append(
                [
                    _item(ids[position], check_value="true", supersedes=marker)
                    for position, marker in enumerate(markers)
                ]
            )
    return contracts


def _non_canonical_id_contracts() -> list[list[dict[str, Any]]]:
    """The same marker space, but with a NON-CANONICAL id on the carrier.

    Two-item contracts ``[dod-0, <carrier>]`` where the carrier's ``id`` is
    each shape in :data:`_NON_CANONICAL_IDS` and its marker ranges over the
    whole marker space (backward, self-ish, absent, none). Kept separate from
    :func:`_small_contracts` so the well-formed domain's arity assertion still
    documents its own size.
    """
    marker_choices: list[str | None] = [None, "dod-0", "dod-1", "dod-absent", ""]
    contracts: list[list[dict[str, Any]]] = []
    for carrier_id in _NON_CANONICAL_IDS:
        for marker in marker_choices:
            contracts.append(
                [
                    _item("dod-0", check_value="true"),
                    _item(carrier_id, check_value="true", supersedes=marker),
                ]
            )
            # ...and with the non-canonical carrier declared FIRST, so the
            # ordering rule is exercised from both sides.
            contracts.append(
                [
                    _item(carrier_id, check_value="true", supersedes=marker),
                    _item("dod-0", check_value="true"),
                ]
            )
    return contracts


def _duplicate_id_contracts() -> list[list[dict[str, Any]]]:
    """Contracts where two items share an ``id`` — the R1 divergence axis.

    ``_superseded_dod_ids`` has NO self-reference branch: it only asks
    ``if supersedes in seen``, and ``seen.add`` happens after that question.
    That is the ONLY reason a self-referential marker is normally inert — the
    carrier's own id is not in ``seen`` yet. Give an EARLIER item the same id
    and ``seen`` does contain it, so the gate retires that earlier entry while
    a runner that tests self-reference first hard-REDs the carrier instead.

    Every id assignment of length 2..3 over a two-symbol alphabet that repeats
    at least one symbol (all-distinct assignments are already covered by
    :func:`_small_contracts`), crossed with the full marker space.
    """
    ids = ["dod-0", "dod-1"]
    marker_choices: list[str | None] = [None, "dod-0", "dod-1", "dod-absent"]
    contracts: list[list[dict[str, Any]]] = []
    for length in (2, 3):
        for assignment in itertools.product(ids, repeat=length):
            if len(set(assignment)) == length:
                continue
            for markers in itertools.product(marker_choices, repeat=length):
                contracts.append(
                    [
                        _item(item_id, check_value="true", supersedes=marker)
                        for item_id, marker in zip(assignment, markers, strict=True)
                    ]
                )
    return contracts


def _parity_domain() -> list[list[dict[str, Any]]]:
    """The full exhaustive domain every parity assertion runs over."""
    return (
        _small_contracts() + _non_canonical_id_contracts() + _duplicate_id_contracts()
    )


@pytest.mark.unit
def test_runner_matches_the_gate_algorithm_over_every_small_contract() -> None:
    """Set-equality with the gate, proven exhaustively over a bounded domain.

    The 11-case corpus is illustrative; this is the invariant. Every contract
    of up to three items over a three-id alphabet, with each item carrying no
    marker, a marker to any of the three ids, or a marker to an absent id —
    including every forward, backward, self and mutually-referential shape —
    must produce the SAME superseded set in the runner as in the gate
    algorithm. The domain also includes carriers whose OWN ``id`` is missing,
    empty, non-string or null, and contracts where two items SHARE an ``id`` —
    the two axes on which the runner has actually diverged. Runs
    unconditionally: no OCC checkout required.
    """
    contracts = _small_contracts()
    assert len(contracts) == 5 + 25 + 125

    non_canonical = _non_canonical_id_contracts()
    assert len(non_canonical) == len(_NON_CANONICAL_IDS) * 5 * 2

    duplicate = _duplicate_id_contracts()
    assert len(duplicate) == 2 * 4**2 + 8 * 4**3

    domain = _parity_domain()
    assert len(domain) == len(contracts) + len(non_canonical) + len(duplicate)

    for contract in domain:
        superseded, _ = _resolve(contract)
        assert superseded == _gate_algorithm_superseded_ids(contract), (
            "runner/gate divergence on "
            f"ids={[i.get('id', '<no id>') for i in contract]} "
            f"markers={[i.get('evidence_artifact') for i in contract]}"
        )


@pytest.mark.unit
def test_the_exhaustive_domain_actually_contains_the_divergence_it_claims() -> None:
    """Guard against the domain being (re)chosen so it cannot fail.

    The previous domain emitted only well-formed string ids, so it could not
    contain the missing/empty/non-string/null-carrier divergence at all — it
    passed while the invariant it named did not hold. This pins that at least
    one contract in the domain BOTH supersedes something AND does so from a
    carrier with an unusable id.
    """
    discriminating = [
        contract
        for contract in _non_canonical_id_contracts()
        if _gate_algorithm_superseded_ids(contract)
    ]
    assert discriminating, "domain no longer exercises non-canonical carriers"
    for contract in discriminating:
        carrier = contract[-1]
        assert not isinstance(carrier.get("id"), str) or not carrier.get("id")


@pytest.mark.unit
def test_the_domain_contains_the_self_reference_on_a_duplicate_id_divergence() -> None:
    """Same guard for the R1 axis: a marker the gate honours BECAUSE of a dup.

    The self-reference/duplicate-id interaction is invisible to any domain with
    unique ids — and the previous domain had unique ids by construction, so the
    "exhaustive" differential could not contain the class at all. This pins that
    the domain still holds a contract in which an item's marker names the item's
    OWN id and the gate nonetheless supersedes, which only happens when an
    earlier item already put that id in ``seen``.
    """
    discriminating = [
        contract
        for contract in _duplicate_id_contracts()
        if any(
            _supersedes_marker(item.get("evidence_artifact")) == item.get("id")
            for item in contract
        )
        and _gate_algorithm_superseded_ids(contract)
    ]
    assert discriminating, (
        "domain no longer contains a self-referential marker that the gate "
        "honours via a duplicate id — the R1 divergence cannot be detected"
    )
    for contract in discriminating:
        ids = [item.get("id") for item in contract]
        assert len(ids) != len(set(ids))


@pytest.mark.unit
def test_a_marker_on_an_id_less_item_is_honoured_exactly_like_the_gate(
    tmp_path: Path,
) -> None:
    """End-to-end, through the real collector: parity is not just set-level.

    A carrier with no usable ``id`` still supersedes, and still has to prove
    itself first — the two rules compose rather than one excusing the other.
    """
    state = _verify(
        tmp_path,
        [
            _item("dod-a", check_value="false"),
            _item(_NO_ID, check_value="true", supersedes="dod-a"),
        ],
    )
    assert _by_id(state)["dod-a"] == EnumEvidenceCheckStatus.SUPERSEDED
    assert state.superseded_count == 1
    assert state.status == EnumDodVerifyStatus.VERIFIED

    unproven = _verify(
        tmp_path,
        [
            _item("dod-a", check_value="false"),
            _item(_NO_ID, supersedes="dod-a"),  # checks: [] -> proves nothing
        ],
    )
    assert _by_id(unproven)["dod-a"] == EnumEvidenceCheckStatus.FAILED
    assert unproven.superseded_count == 0
    assert unproven.status == EnumDodVerifyStatus.FAILED


@pytest.mark.unit
def test_a_broken_marker_on_an_id_less_item_is_not_a_silent_skip(
    tmp_path: Path,
) -> None:
    """Fail-closed reporting must not depend on the carrier having an ``id``.

    Keying the malformed map by id made this exact case a silent no-op: the
    marker resolved to nothing, nothing was reported, and the contract read
    clean. It is reported against the carrier's POSITION instead.
    """
    state = _verify(
        tmp_path,
        [
            _item("dod-a", check_value="true"),
            _item(_NO_ID, check_value="true", supersedes="dod-typo"),
        ],
    )
    statuses = _by_id(state)

    assert statuses["dod_evidence[1]"] == EnumEvidenceCheckStatus.FAILED
    assert state.status == EnumDodVerifyStatus.FAILED
    carrier = next(c for c in state.checks if c.evidence_id == "dod_evidence[1]")
    assert "DANGLING_SUPERSESSION" in carrier.message


# ---------------------------------------------------------------------------
# The visited-set guard in ``_terminal_superseder`` is LOAD-BEARING.
#
# R1 reordered ``_resolve_supersessions`` so ``target in seen`` is tested before
# ``target == item_id_str``, which is what the OCC gate does (it has no
# self-reference branch at all). A necessary consequence: when an EARLIER item
# already declared the carrier's own id, the carrier's self-referential marker
# is an ACCEPTED edge — and because ``superseded`` is keyed by ID while the
# backwards-ordering property holds by INDEX, that edge is an id-level
# SELF-LOOP. The relation the pre-R1 comments called "acyclic by construction"
# is not acyclic, and the guard those comments called "a defensive backstop,
# not a live path" is the only thing that terminates the walk.
#
# Those comments were left unchanged by the PR that made them false. These
# tests exist so the claim cannot silently rot again in either direction.
# ---------------------------------------------------------------------------


def _unguarded_terminal_superseder(
    target_id: str,
    superseded: dict[str, int],
    id_at: dict[int, str | None],
    budget: int = 10_000,
) -> int | None:
    """``_terminal_superseder`` with the visited-set guard REMOVED.

    Returns the terminal index, or ``None`` if it failed to terminate within
    ``budget`` steps. Step-bounded so this test documents the hang instead of
    reproducing it.
    """
    index = superseded[target_id]
    for _ in range(budget):
        carrier_id = id_at.get(index)
        if carrier_id is None or carrier_id not in superseded:
            return index
        index = superseded[carrier_id]
    return None


def _run_bounded(source: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    """Execute ``source`` in a fresh interpreter under a hard timeout.

    Every assertion that drives ``_terminal_superseder`` over a SELF-LOOPING
    contract goes through here. If the visited-set guard is ever deleted, an
    in-process call hangs forever — and because ``_collect_impl`` phase 2 calls
    the walk, that is not confined to the helper: the whole ``collect()`` path
    hangs, so an in-process test would wedge the ENTIRE suite (and the CI job)
    rather than failing one case. Verified by executing exactly that: with the
    guard removed, this module's end-to-end self-loop case did not return in
    5 minutes. The subprocess converts the hang into a normal test failure.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


_GUARD_REMOVED_HINT = (
    "This hangs only if ``_terminal_superseder``'s visited-set guard was "
    "removed. It is REQUIRED, not defensive: duplicate ids admit an id-level "
    "self-loop and the guard is the only thing that terminates the walk."
)


def _guard_fire_census(
    contracts: list[list[dict[str, Any]]],
) -> tuple[int, int, int]:
    """(edges walked, walks whose visited-set guard fired, id-level self-loops)."""
    edges = fires = self_loops = 0
    for contract in contracts:
        resolution = EvidenceCollector._resolve_supersessions(contract)
        superseded = resolution.superseded
        id_at: dict[int, str | None] = {
            index: (
                item.get("id")
                if isinstance(item.get("id"), str) and item.get("id")
                else None
            )
            for index, item in enumerate(contract)
        }
        for target_id in superseded:
            edges += 1
            # The guard fired iff the unguarded walk cannot terminate.
            if _unguarded_terminal_superseder(target_id, superseded, id_at) is None:
                fires += 1
            if id_at.get(superseded[target_id]) == target_id:
                self_loops += 1
    return edges, fires, self_loops


@pytest.mark.unit
class TestTheVisitedSetGuardIsRequiredNotDefensive:
    """Pins the guard as live, load-bearing code — the opposite of what the
    ``_terminal_superseder`` docstring asserted before this change."""

    def test_a_duplicate_id_admits_an_id_level_self_loop(self) -> None:
        """The precondition: resolution really does record ``n -> n``.

        Not hypothetical, and not a malformed-marker case — ``malformed`` is
        empty, because this is exactly what the gate accepts.
        """
        contract = [
            _item("dod-0", check_value="true"),
            _item("dod-0", check_value="true", supersedes="dod-0"),
        ]
        resolution = EvidenceCollector._resolve_supersessions(contract)
        assert resolution.superseded == {"dod-0": 1}
        assert resolution.malformed == {}, (
            "a self-referential marker on a duplicate id must NOT be malformed "
            "— the gate has no self-reference branch, so hard-REDing it here "
            "is the runner-stricter-than-gate bug class R1 removed"
        )
        # id_at[1] == 'dod-0' and superseded['dod-0'] == 1: the walk maps 1->1.
        assert contract[resolution.superseded["dod-0"]]["id"] == "dod-0"

    def test_the_walk_does_not_terminate_without_the_guard(self) -> None:
        """Why the guard cannot be deleted: the same input loops forever."""
        assert (
            _unguarded_terminal_superseder(
                "dod-0", {"dod-0": 1}, {0: "dod-0", 1: "dod-0"}
            )
            is None
        ), "expected the unguarded walk to spin on the id-level self-loop"

    def test_the_real_walk_terminates_on_the_self_loop(self) -> None:
        """The shipped function terminates, in a SUBPROCESS with a timeout.

        Deliberately not called in-process — see :func:`_run_bounded`.
        """
        try:
            completed = _run_bounded(
                """
                from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
                    EvidenceCollector,
                )

                print(
                    EvidenceCollector._terminal_superseder(
                        "dod-0", {"dod-0": 1}, {0: "dod-0", 1: "dod-0"}
                    )
                )
                """
            )
        except subprocess.TimeoutExpired:  # pragma: no cover - only if guard removed
            pytest.fail(
                f"_terminal_superseder did not terminate. {_GUARD_REMOVED_HINT}"
            )
        assert completed.returncode == 0, completed.stderr
        # Returns the revisited index; the caller then finds nothing in
        # ``executed`` for it, so the edge does not fire (fail-safe).
        assert completed.stdout.strip() == "1"

    def test_the_guard_is_dead_on_unique_ids_and_live_on_duplicates(self) -> None:
        """The measurement that makes "not a live path" a checkable claim.

        The old docstring was true of the domain the code was tested against
        and false of the domain R1 added. Pinning both numbers means neither
        the claim nor the domain can drift without this failing.
        """
        unique_edges, unique_fires, unique_loops = _guard_fire_census(
            _small_contracts()
        )
        assert (unique_edges, unique_fires, unique_loops) == (75, 0, 0), (
            "unique-id domain should never exercise the guard; got "
            f"{unique_edges} edges / {unique_fires} fires / {unique_loops} loops"
        )

        dup_edges, dup_fires, dup_loops = _guard_fire_census(_duplicate_id_contracts())
        assert (dup_edges, dup_fires, dup_loops) == (296, 176, 152), (
            "duplicate-id domain guard census drifted; got "
            f"{dup_edges} edges / {dup_fires} fires / {dup_loops} loops"
        )
        assert dup_fires / dup_edges > 0.5, (
            "the guard fires on a MAJORITY of duplicate-id edges — it is live "
            "code, not a backstop"
        )

    def test_a_self_looping_contract_still_produces_a_fail_safe_receipt(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: the self-loop degrades to "nobody is retired", not a hang.

        The carrier is itself a supersession target, so phase 1 never executes
        it, ``executed.get(carrier)`` is ``None``,
        ``_supersession_is_in_effect`` is False and the edge does not fire.
        Both entries execute and carry their own verdicts.

        Driven through the real collector + handler + ``_build_receipt``, but
        in a bounded subprocess: ``_collect_impl`` phase 2 calls the walk, so
        this is the case that hangs the suite if the guard goes away.
        """
        ticket_id, contract_path = _write_contract(
            tmp_path,
            [
                _item("dod-0", check_value="false"),
                _item("dod-0", check_value="true", supersedes="dod-0"),
            ],
        )
        try:
            completed = _run_bounded(
                f"""
                import json
                from pathlib import Path

                from omnimarket.nodes.node_dod_verify.__main__ import _build_receipt
                from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
                    HandlerDodVerify,
                )
                from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
                    ModelDodVerifyStartCommand,
                )
                from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
                    EvidenceCollector,
                )

                contract_path = {str(contract_path)!r}
                results = EvidenceCollector().collect(
                    {ticket_id!r}, contract_path=contract_path
                )
                state = HandlerDodVerify()._handle_typed(
                    ModelDodVerifyStartCommand(
                        ticket_id={ticket_id!r}, contract_path=contract_path
                    ),
                    evidence_results=results,
                )
                print(
                    json.dumps(
                        {{
                            "superseded_count": state.superseded_count,
                            "status": state.status.value,
                            "receipt": _build_receipt(
                                state, None, Path({str(tmp_path)!r})
                            )["status"],
                        }}
                    )
                )
                """
            )
        except subprocess.TimeoutExpired:  # pragma: no cover - only if guard removed
            pytest.fail(f"collect() did not terminate. {_GUARD_REMOVED_HINT}")
        assert completed.returncode == 0, completed.stderr
        outcome = json.loads(completed.stdout.strip().splitlines()[-1])
        assert outcome["superseded_count"] == 0, (
            "a self-looping edge must retire nothing — otherwise a duplicate "
            "id becomes a laundering primitive"
        )
        assert outcome["status"] == EnumDodVerifyStatus.FAILED.value
        assert outcome["receipt"] == "FAIL"


@pytest.mark.unit
def test_a_superseded_entry_always_implies_a_verified_carrier_across_the_domain(
    tmp_path: Path,
) -> None:
    """The invariant that makes the handler's global backstop unreachable.

    ``HandlerDodVerify._handle_typed`` keeps a ``superseded > 0 and
    verified == 0`` branch for the caller-supplied-results path. Via the
    collector it must be dead code, because an edge only fires when its
    carrier VERIFIED. Asserted over the WHOLE non-canonical + duplicate-id
    domain — every contract is executed end-to-end through the real collector,
    handler and ``_build_receipt``.

    This test previously truncated the domain at ``[:20]``, which stopped
    exactly one contract short of the ``id: 7`` and ``id: None`` carriers.
    Those aborted ``collect()`` with an unhandled ``ValidationError`` and
    emitted NO receipt at all, so the truncation was not a cost saving — it was
    the only reason the suite looked green. The docstring claimed "the whole
    executable domain" while covering half of it.

    Cost, stated rather than hidden: 584 contracts executed end-to-end with
    real ``subprocess`` checks is roughly a minute of wall clock, and that is
    the single most expensive test in this module. It is worth it — this is the
    only place the duplicate-id shapes are driven through the two-phase
    executor, including the ones where an item is its own supersession target
    and ``_terminal_superseder`` has to terminate on its visited-set guard. If
    it ever needs to shrink, shrink it by making the checks cheaper, never by
    slicing the domain.
    """
    for contract in _non_canonical_id_contracts() + _duplicate_id_contracts():
        state = _verify(tmp_path, contract)
        if state.superseded_count > 0:
            assert state.verified_count > 0, (
                "superseded entry with no verified carrier: "
                f"{[i.get('evidence_artifact') for i in contract]}"
            )
        # Whatever the shape, a receipt is always produced — the run never
        # aborts (OMN-15390 residual R2).
        assert _build_receipt(state, None, tmp_path)["status"] in {"PASS", "FAIL"}


@pytest.mark.unit
class TestAContractTheReceiptCannotRepresentFailsClosed:
    """OMN-15390 residual R2 — no dod_evidence shape may abort ``collect()``.

    ``ModelEvidenceCheckResult.evidence_id`` and ``.description`` are typed
    ``str``. A contract carrying ``id: 7`` / ``id: null``, a ``dod_evidence``
    element that is not a mapping, a non-string ``description``, or a
    ``checks`` element that is not a mapping raised ``ValidationError`` /
    ``AttributeError`` straight out of ``collect()``: the process died and NO
    receipt was written. On the only sanctioned Done-flip path, "no receipt"
    is strictly worse than a FAIL receipt — nothing downstream can distinguish
    it from work that was never attempted. Every shape now fails CLOSED with a
    receipted verdict naming the offending position.
    """

    @pytest.mark.parametrize("bad_id", [7, None, ["dod-a"], {"id": "dod-a"}, True])
    def test_a_non_string_id_is_a_receipted_failure_not_an_abort(
        self, tmp_path: Path, bad_id: Any
    ) -> None:
        state = _verify(tmp_path, [_item(bad_id, check_value="true")])

        assert _by_id(state)["dod_evidence[0]"] == EnumEvidenceCheckStatus.FAILED
        assert state.status == EnumDodVerifyStatus.FAILED
        entry = next(c for c in state.checks if c.evidence_id == "dod_evidence[0]")
        assert "MALFORMED_EVIDENCE_ID" in (entry.message or "")
        assert _build_receipt(state, None, tmp_path)["status"] == "FAIL"

    def test_a_non_mapping_evidence_entry_is_a_receipted_failure(
        self, tmp_path: Path
    ) -> None:
        state = _verify(tmp_path, ["not a mapping"])  # type: ignore[list-item]

        assert _by_id(state)["dod_evidence[0]"] == EnumEvidenceCheckStatus.FAILED
        entry = next(c for c in state.checks if c.evidence_id == "dod_evidence[0]")
        assert "MALFORMED_EVIDENCE_ITEM" in (entry.message or "")
        assert _build_receipt(state, None, tmp_path)["status"] == "FAIL"

    def test_a_bad_id_cannot_launder_its_siblings_into_a_pass(
        self, tmp_path: Path
    ) -> None:
        """The rejected entry must drag the RECEIPT down, not be skipped."""
        state = _verify(
            tmp_path,
            [
                _item("dod-good", check_value="true"),
                _item(7, check_value="true"),
            ],
        )

        assert _by_id(state)["dod-good"] == EnumEvidenceCheckStatus.VERIFIED
        assert _by_id(state)["dod_evidence[1]"] == EnumEvidenceCheckStatus.FAILED
        assert _build_receipt(state, None, tmp_path)["status"] == "FAIL"

    def test_a_bad_id_on_a_superseder_retires_nothing(self, tmp_path: Path) -> None:
        """Rejection composes with the anti-laundering rule.

        The gate's superseded SET still contains ``dod-a`` (parity is asserted
        over the exhaustive domain), but the carrier never proved anything, so
        the edge does not fire and ``dod-a`` executes and fails on its own.
        """
        state = _verify(
            tmp_path,
            [
                _item("dod-a", check_value="false"),
                _item(7, check_value="true", supersedes="dod-a"),
            ],
        )

        assert _by_id(state)["dod-a"] == EnumEvidenceCheckStatus.FAILED
        assert state.superseded_count == 0
        assert _build_receipt(state, None, tmp_path)["status"] == "FAIL"

    @pytest.mark.parametrize(
        ("field", "value"),
        [("description", {"k": "v"}), ("checks", ["true"])],
    )
    def test_an_unforeseen_item_defect_still_produces_a_receipt(
        self, tmp_path: Path, field: str, value: Any
    ) -> None:
        """The fail-closed boundary in ``_execute_item``, not the known-shape list.

        These two shapes are NOT rejected up front — they reach execution and
        blow up inside it. They must still land as a receipted FAILED entry.
        """
        item = _item("dod-a", check_value="true")
        item[field] = value

        state = _verify(tmp_path, [item])

        assert _by_id(state)["dod-a"] == EnumEvidenceCheckStatus.FAILED
        assert state.status == EnumDodVerifyStatus.FAILED
        assert _build_receipt(state, None, tmp_path)["status"] == "FAIL"


@pytest.mark.unit
def test_occ_gate_agrees_with_the_runner_on_the_shared_corpus() -> None:
    """Live cross-repo differential against the OCC gate's REAL implementation.

    Imports ``contract_compliance_check._superseded_dod_ids`` from an OCC
    checkout and runs it over the same corpus.

    OMN-15390 residual R3: hosted CI USED to have no OCC checkout, so this
    skipped there and the only cross-repo differential in the repo never ran on
    a PR. It now runs in CI too — ``ci.yml``'s ``test`` job checks
    ``OmniNode-ai/onex_change_control`` (a PUBLIC repo, so the default
    ``github.token`` suffices; no new secret or permission) out into
    :data:`_IN_WORKSPACE_OCC`, exactly as the ``contract-compliance`` and
    ``onex-schema-compat`` jobs already do. That wiring is itself pinned by
    :func:`test_ci_wires_an_occ_checkout_into_the_job_that_runs_this_suite`, so
    deleting the step turns THIS file red rather than quietly restoring the
    skip. The skip survives only for environments with no checkout at all
    (e.g. the coverage-sweep job); a checkout that is present but unusable
    FAILS rather than skipping.
    """
    import importlib
    import os
    import sys

    roots = [
        # In-workspace checkout — what ci.yml provides. First, and deliberately
        # not an env var: exporting ONEX_CC_REPO_PATH for the whole test job
        # would re-point `_resolve_contract_repo_dir` for every other test in
        # the suite (the OMN-15382 hermeticity trap).
        str(_IN_WORKSPACE_OCC),
        os.environ.get("ONEX_CC_REPO_PATH", "").strip(),
        str(Path(os.environ.get("OMNI_HOME", "")) / "onex_change_control"),
    ]
    src_root: Path | None = None
    for root in roots:
        if not root:
            continue
        candidate = Path(root) / "src"
        if (
            candidate
            / "onex_change_control"
            / "scripts"
            / "contract_compliance_check.py"
        ).is_file():
            src_root = candidate
            break
    if src_root is None:
        pytest.skip(
            "OCC checkout not present (expected at "
            f"{_IN_WORKSPACE_OCC}, or set ONEX_CC_REPO_PATH / OMNI_HOME) — "
            "cross-repo parity differential not run; "
            "test_runner_matches_the_gate_algorithm_over_every_small_contract "
            "still holds the invariant here."
        )

    # Prepending ``src_root`` to ``sys.path`` is NOT sufficient on its own, and
    # this is the trap that made the first CI run of this test red rather than
    # green. omnimarket DEPENDS on onex-change-control (`pyproject.toml` pins it
    # to a git rev), and that INSTALLED distribution ships
    # `onex_change_control/scripts/` WITHOUT `contract_compliance_check.py` and
    # has no `validation.evidence_admissibility` at all. Once any earlier test
    # in the session has imported `onex_change_control`, the parent package is
    # bound to site-packages and every submodule lookup follows ITS `__path__`,
    # which `sys.path` order cannot override — so the differential either
    # resolved the wrong tree or failed outright, depending on test ordering.
    #
    # Purge the cached OCC package tree, import the whole thing from the
    # CHECKOUT, then restore site-packages' modules so nothing else in the
    # session sees a swapped dependency.
    occ_prefix = "onex_change_control"
    saved_modules = {
        name: mod
        for name, mod in sys.modules.items()
        if name == occ_prefix or name.startswith(f"{occ_prefix}.")
    }
    for name in saved_modules:
        del sys.modules[name]
    sys.path.insert(0, str(src_root))
    try:
        module = importlib.import_module(
            "onex_change_control.scripts.contract_compliance_check"
        )
    except ImportError as exc:  # pragma: no cover - environment dependent
        # NOT a skip. A checkout that exists but cannot be imported is a broken
        # differential, and skipping on it is how a wired gate silently becomes
        # advisory again.
        pytest.fail(
            f"OCC checkout at {src_root} is present but "
            f"contract_compliance_check is not importable ({exc}). The "
            "cross-repo parity differential cannot be silently skipped once a "
            "checkout exists — fix the checkout or the dependency."
        )
    finally:
        if str(src_root) in sys.path:
            sys.path.remove(str(src_root))
        for name in [
            name
            for name in sys.modules
            if name == occ_prefix or name.startswith(f"{occ_prefix}.")
        ]:
            del sys.modules[name]
        sys.modules.update(saved_modules)

    assert Path(module.__file__ or "").is_relative_to(src_root), (
        f"resolved {module.__file__!r}, which is not inside the checkout at "
        f"{src_root} — the differential would be comparing against the "
        "installed onex-change-control distribution, not the live gate"
    )

    occ_superseded_ids = module._superseded_dod_ids

    for case in _corpus_cases():
        expected = set(case["expected_superseded"])
        assert occ_superseded_ids(case["dod_evidence"]) == expected, (
            f"OCC gate disagrees with the shared corpus on {case['name']!r}"
        )
        superseded, _ = _resolve(case["dod_evidence"])
        assert superseded == expected

    # Pin the transcribed oracle to the real function over the whole
    # exhaustive domain, not just the corpus — including the non-canonical
    # carrier-id shapes and the duplicate-id shapes, the two axes on which the
    # runner has diverged from the REAL function while the transcribed oracle
    # agreed with both.
    for contract in _parity_domain():
        assert occ_superseded_ids(contract) == _gate_algorithm_superseded_ids(contract)
        superseded, _ = _resolve(contract)
        assert superseded == occ_superseded_ids(contract), (
            "runner diverges from the REAL OCC gate on "
            f"{[i.get('id', '<no id>') for i in contract]} / "
            f"{[i.get('evidence_artifact') for i in contract]}"
        )


@pytest.mark.unit
def test_ci_wires_an_occ_checkout_into_the_job_that_runs_this_suite() -> None:
    """Static wiring pin for R3 — the differential above must RUN in hosted CI.

    A cross-repo differential that skips on every PR is documentation, not a
    gate. The mechanism is a checkout step in ``ci.yml``'s ``test`` job; this
    is the check that keeps it there. Static and offline: it reads the workflow
    file, it does not call GitHub.
    """
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]

    checkouts = [
        step
        for step in steps
        if str(step.get("uses", "")).startswith("actions/checkout")
        and (step.get("with") or {}).get("repository")
        == "OmniNode-ai/onex_change_control"
    ]
    assert checkouts, (
        "ci.yml's `test` job no longer checks out onex_change_control — "
        "test_occ_gate_agrees_with_the_runner_on_the_shared_corpus would go "
        "back to skipping on every PR, leaving the OCC parity claim unproven "
        "in CI."
    )
    path = (checkouts[0].get("with") or {}).get("path")
    assert path == _IN_WORKSPACE_OCC.name, (
        f"OCC checkout path is {path!r} but the parity test looks for "
        f"{_IN_WORKSPACE_OCC.name!r} at the workspace root"
    )
