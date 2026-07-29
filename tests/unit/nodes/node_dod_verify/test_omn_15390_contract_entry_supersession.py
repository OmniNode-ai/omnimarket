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

RED-before / GREEN-after (recorded, both directions):

* Reverting the ordering rule in ``_resolve_supersessions`` to the position-blind
  whole-contract lookup turns ``TestOrderingParityWithTheGate`` and the
  exhaustive differential RED (the forward-marker cases diverge from the gate).
* Reverting ``total_checks=non_superseded_total`` to ``len(checks)`` turns
  ``test_total_checks_is_the_verdict_bearing_denominator`` RED.
* Reverting the ``superseded > 0 and verified == 0`` branch turns
  ``test_marker_that_proves_nothing_cannot_launder_a_fail_into_a_pass_receipt``
  RED — that assertion is exactly the fail-open an adversarial review found.
"""

from __future__ import annotations

import itertools
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

_MARKER_PREFIX = "supersedes_dod_evidence:"


# ---------------------------------------------------------------------------
# Helpers — these drive the REAL collector and the REAL handler, never a
# surrogate. ``_verify`` is the same path ``onex skill dod_verify`` takes.
# ---------------------------------------------------------------------------


def _item(
    item_id: str,
    *,
    check_value: str | None = None,
    supersedes: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"id": item_id, "description": f"item {item_id}"}
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
    """(superseded ids, ids carrying a marker that resolved to nothing)."""
    resolution = EvidenceCollector._resolve_supersessions(dod_evidence)
    return set(resolution.superseded), set(resolution.malformed)


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
        retires a genuinely-failing entry and drops ``failed_count`` to 0. If
        the verdict were merely "not FAILED" that would receipt as PASS: a
        strictly worse outcome than the FAIL it replaced, on the only
        sanctioned Done-flip path. Supersession may remove a FALSE red; it may
        never manufacture a green.
        """
        without_marker = _verify(tmp_path, [_item("dod-a", check_value="false")])
        assert without_marker.status == EnumDodVerifyStatus.FAILED
        assert _build_receipt(without_marker, None, tmp_path)["status"] == "FAIL"

        with_marker = _verify(
            tmp_path,
            [
                _item("dod-a", check_value="false"),
                _item("dod-b", supersedes="dod-a"),
            ],
        )
        assert with_marker.superseded_count == 1
        assert with_marker.verified_count == 0
        assert with_marker.status == EnumDodVerifyStatus.FAILED
        assert _build_receipt(with_marker, None, tmp_path)["status"] == "FAIL"


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


@pytest.mark.unit
def test_runner_matches_the_gate_algorithm_over_every_small_contract() -> None:
    """Set-equality with the gate, proven exhaustively over a bounded domain.

    The 11-case corpus is illustrative; this is the invariant. Every contract
    of up to three items over a three-id alphabet, with each item carrying no
    marker, a marker to any of the three ids, or a marker to an absent id —
    including every forward, backward, self and mutually-referential shape —
    must produce the SAME superseded set in the runner as in the gate
    algorithm. Runs unconditionally: no OCC checkout required.
    """
    contracts = _small_contracts()
    assert len(contracts) == 5 + 25 + 125

    for contract in contracts:
        superseded, _ = _resolve(contract)
        assert superseded == _gate_algorithm_superseded_ids(contract), (
            f"runner/gate divergence on {[i.get('evidence_artifact') for i in contract]}"
        )


@pytest.mark.unit
def test_occ_gate_agrees_with_the_runner_on_the_shared_corpus() -> None:
    """Live cross-repo differential against the OCC gate's REAL implementation.

    Imports ``contract_compliance_check._superseded_dod_ids`` from the workspace
    OCC checkout and runs it over the same corpus. Hosted CI has no such
    checkout, so this SKIPS LOUDLY there rather than passing vacuously — the
    exhaustive test above is what holds the invariant in CI, and this is what
    pins its transcribed oracle to the real function locally and on ``.200``.
    """
    import importlib
    import os
    import sys

    roots = [
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
            "OCC checkout not present (set ONEX_CC_REPO_PATH or OMNI_HOME) — "
            "cross-repo parity differential not run; "
            "test_runner_matches_the_gate_algorithm_over_every_small_contract "
            "still holds the invariant here."
        )

    inserted = str(src_root) not in sys.path
    if inserted:
        sys.path.insert(0, str(src_root))
    try:
        module = importlib.import_module(
            "onex_change_control.scripts.contract_compliance_check"
        )
    except ImportError as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            "OCC checkout present but not importable in this environment "
            f"({exc}) — cross-repo parity differential not run."
        )
    finally:
        if inserted and str(src_root) in sys.path:
            sys.path.remove(str(src_root))

    occ_superseded_ids = module._superseded_dod_ids

    for case in _corpus_cases():
        expected = set(case["expected_superseded"])
        assert occ_superseded_ids(case["dod_evidence"]) == expected, (
            f"OCC gate disagrees with the shared corpus on {case['name']!r}"
        )
        superseded, _ = _resolve(case["dod_evidence"])
        assert superseded == expected

    # Pin the transcribed oracle to the real function over the whole
    # exhaustive domain, not just the corpus.
    for contract in _small_contracts():
        assert occ_superseded_ids(contract) == _gate_algorithm_superseded_ids(contract)
