# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

r"""OMN-15391 — a surrogate check must not count toward completion.

A ``dod_evidence`` item has two independent surfaces: the **description** (prose
a human reads to decide whether the bar was met) and the **check_value** (the
command a machine runs). Nothing asserted they describe the same proof. Every
gate we have reads only the command, and every one of them asks "is this command
real?" — never "does this command have anything to do with this ticket?".

So a command that is genuinely executed and genuinely falsifiable could stand in
for a proof it has nothing to do with, pass every gate, and prove nothing. The
ticket's own instance: ``contracts/OMN-15376.yaml`` narrated a Postgres-16
experiment over 74 unfenced migrations, and the command underneath ran OCC's own
admissibility suite — ``76 passed in 0.13s``, green with the entire OMN-15376 fix
reverted.

This module pins the class at the surface that decides completion. It is scoped
to the sub-class that is vacuous **by construction** rather than by judgement:
a command whose exit status is invariant over the product diff. See
``omnimarket.occ_evidence_probative_class`` for why the asserted-PR-state
reading is deliberately excluded.

RED before the fix — measured by execution 2026-08-27 against the unmodified
collector + handler at ``origin/dev`` ``6a485f59``, on the VERBATIM pinned
contracts, with subprocesses stubbed to the exit status those commands really
return in the fleet:

* ``contracts/OMN-16667.yaml`` -> ``verified=5``, ``contracts/OMN-16620.yaml``
  -> ``verified=7``. Every declared check counted as completion, and not one of
  them can go red for any product reason. (Both runs also showed live-PR-state
  legs red under that crude stub, so the recorded ``status`` was FAILED; the
  load-bearing RED is the ``verified`` tally, and the tests below stub those
  legs GREEN — the strictly harder case.)
* ``9 failed, 27 passed`` for this module as a whole on that commit.
* Every ``EnumEvidenceCheckStatus.NON_PROBATIVE`` reference — ``AttributeError``.

The corpus assertions read ``omn_15391_occ_surrogate_corpus_pinned``, a verbatim
snapshot of ``onex_change_control`` at a named SHA. AC1 refuses a gate that only
passes on a hand-built fixture, and a live-clone read does not run on a hosted
runner; the pin is how both hold at once (same construction as
``omn_15597_occ_census_pinned``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

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
    ModelEvidenceCheckResult,
)
from omnimarket.occ_evidence_probative_class import (
    EnumEvidenceProbativeClass,
    classify_check_value,
    is_surrogate_check_value,
)

from .omn_15391_occ_surrogate_corpus_pinned import (
    CONTENT_BOUND_CLASSIFIED_PROBATIVE,
    CONTENT_BOUND_TOTAL,
    NAMED_VALIDATION_WAVE_CONTRACTS,
    PINNED_CLASS_COUNTS,
    ZERO_PROBATIVE_CONTRACTS,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# The predicate itself
# --------------------------------------------------------------------------


# Long verbatim commands are bound to names rather than written as implicit
# adjacent-string concatenation inside the parametrize lists below: CodeQL flags
# that shape as a possible missing comma, and in a list of commands a silently
# joined pair would change what is under test without any test going red.
_PR_VIEW_MULTI_FIELD = (
    "gh pr view 6931 --repo OmniNode-ai/onex_change_control "
    "--json number,state,headRefName"
)
_CONTENT_BOUND_READ = (
    "gh api repos/OmniNode-ai/omnibase_infra/contents/src/x.py"
    "?ref=bfa0b093646471667a265e4d884af53857fa2e10 --jq '.content' "
    "| base64 -d | grep -c 'def _require_str'"
)
_ASSERTED_PR_STATE = (
    "gh pr view 6931 --repo OmniNode-ai/onex_change_control --json state "
    "--jq '.state' | grep -q MERGED"
)


class TestTheSurrogatePredicate:
    """``classify_check_value`` over the shapes that motivated the ticket."""

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr view 324 --repo OmniNode-ai/omniweb --json number,state",
            "gh pr view 1015 --repo OmniNode-ai/omninode_infra --json files",
            _PR_VIEW_MULTI_FIELD,
            # The runner substitutes ``${PR_NUMBER}``/``${REPO}`` before
            # execution; a value placeholder is not an assertion, so the
            # template classifies exactly as its resolved form does.
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state",
        ],
    )
    def test_a_bare_gh_pr_view_is_a_pr_state_surrogate(self, command: str) -> None:
        """``gh pr view`` exits 0 for every visible PR. It cannot bear a verdict.

        ``--json`` selects what is printed to stdout; nothing reads stdout, and
        the exit status does not move. The command is green before the product
        change, after it, and with it reverted.
        """
        assert (
            classify_check_value(command)
            is EnumEvidenceProbativeClass.PR_STATE_SURROGATE
        )

    def test_occ_s_own_admissibility_suite_is_a_foreign_suite_surrogate(self) -> None:
        """The ticket's headline instance: OCC's predicate suite as ticket proof."""
        assert (
            classify_check_value(
                "uv run pytest tests/test_evidence_admissibility.py -q"
            )
            is EnumEvidenceProbativeClass.FOREIGN_SUITE_SURROGATE
        )

    @pytest.mark.parametrize(
        "command",
        [
            # A content read pinned at an immutable ref — RED at the merge base,
            # GREEN at the head. The one generated shape that observes the product.
            _CONTENT_BOUND_READ,
            # AC4's negative control, stated as the ticket words it: a
            # legitimately generic repo-wide invariant suite that a ticket
            # actually changed must NOT be flagged.
            "uv run pytest tests/test_architectural_invariants.py -q",
            "uv run pytest tests/unit/nodes/node_dod_verify -q",
            "uv run mypy src/omnimarket --strict",
            # An ASSERTED PR-state probe. Deliberately out of scope — it can go
            # red, and for a ticket whose DoD genuinely is "the PR merged" it is
            # the correct evidence. Separating those readings needs the ticket's
            # own bar (OMN-14409), not this predicate.
            _ASSERTED_PR_STATE,
            'test "$(gh pr view 12 --repo o/r --json state --jq .state)" = MERGED',
        ],
    )
    def test_a_probative_command_is_never_flagged(self, command: str) -> None:
        """No false positives on anything whose exit status can move."""
        assert classify_check_value(command) is EnumEvidenceProbativeClass.PROBATIVE

    @pytest.mark.parametrize("value", [None, "", "   ", 17, ["gh", "pr", "view"]])
    def test_a_malformed_check_value_is_left_alone(self, value: object) -> None:
        """A malformed check is a different defect; the collector hard-fails it.

        This predicate must never be the thing that turns a malformed check into
        a quietly tolerated one, so it declines to classify and leaves the
        existing shape guards to fail it.
        """
        assert (
            classify_check_value(value)  # type: ignore[arg-type]
            is EnumEvidenceProbativeClass.PROBATIVE
        )


# --------------------------------------------------------------------------
# AC1 / AC3 / AC4 — the live corpus, pinned
# --------------------------------------------------------------------------


class TestAgainstTheLiveCorpus:
    """The predicate flags the real instances, and only those."""

    @pytest.mark.parametrize("ticket", sorted(NAMED_VALIDATION_WAVE_CONTRACTS))
    def test_the_named_contracts_classify_exactly_as_snapshotted(
        self, ticket: str
    ) -> None:
        """AC1: flag the real items, verbatim, before any of them is edited."""
        for evidence_id, check_value, expected in NAMED_VALIDATION_WAVE_CONTRACTS[
            ticket
        ]:
            assert classify_check_value(check_value).value == expected, (
                f"{ticket} / {evidence_id}: {check_value!r}"
            )

    @pytest.mark.parametrize("ticket", ["OMN-16620", "OMN-16667"])
    def test_the_all_surrogate_contracts_have_no_probative_check_at_all(
        self, ticket: str
    ) -> None:
        """The false-green, stated at full strength.

        Both contracts read fully verified on the 2026-08-27 validation wave and
        were INCOMPLETE on direct readback. Not one of their checks can go red
        for any product reason — the tally was carried entirely by provenance.
        """
        rows = NAMED_VALIDATION_WAVE_CONTRACTS[ticket]
        assert rows, ticket
        assert all(is_surrogate_check_value(cv) for _, cv, _ in rows)
        assert ticket in ZERO_PROBATIVE_CONTRACTS

    def test_the_mixed_contracts_keep_their_real_evidence(self) -> None:
        """A contract with real proof keeps it — only the surrogates are demoted."""
        for ticket, expected_probative in (
            ("OMN-15570", 2),
            ("OMN-16162", 2),
            ("OMN-15797", 5),
            ("OMN-15376", 19),
        ):
            rows = NAMED_VALIDATION_WAVE_CONTRACTS[ticket]
            probative = [cv for _, cv, _ in rows if not is_surrogate_check_value(cv)]
            assert len(probative) == expected_probative, ticket

    def test_no_false_positive_on_any_content_bound_command_in_the_corpus(self) -> None:
        """AC4, measured over the corpus rather than argued.

        ``?ref=`` is the marker the producer itself keys "product-observing" on
        (``occ_evidence_stamp.is_product_observing_check_value``). Every one of
        them in the live corpus stays PROBATIVE.
        """
        assert CONTENT_BOUND_CLASSIFIED_PROBATIVE == CONTENT_BOUND_TOTAL
        assert CONTENT_BOUND_TOTAL > 1000, "corpus pin looks truncated"

    def test_the_corpus_is_mostly_probative(self) -> None:
        """A predicate that flagged most of the corpus would be the wrong one."""
        flagged = (
            PINNED_CLASS_COUNTS["pr_state_surrogate"]
            + PINNED_CLASS_COUNTS["foreign_suite_surrogate"]
        )
        total = flagged + PINNED_CLASS_COUNTS["probative"]
        assert flagged / total < 0.20, f"{flagged}/{total} flagged — too broad"
        assert len(ZERO_PROBATIVE_CONTRACTS) == 347


# --------------------------------------------------------------------------
# The verify-side refusal — the load-bearing behaviour
# --------------------------------------------------------------------------


def _state(checks: list[ModelEvidenceCheckResult]) -> ModelDodVerifyState:
    command = ModelDodVerifyStartCommand(
        correlation_id=uuid.uuid4(),
        ticket_id="OMN-15391",
        requested_at=datetime.now(tz=UTC),
    )
    result = HandlerDodVerify()._handle_typed(command, checks)
    assert isinstance(result, ModelDodVerifyState)
    return result


def _check(
    evidence_id: str, status: EnumEvidenceCheckStatus
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id, description=evidence_id, status=status
    )


class TestTheVerdictRefusesToCountASurrogate:
    """A non-probative result is admitted, reported, and never counted."""

    def test_non_probative_is_a_distinct_terminal_status(self) -> None:
        """It is neither a pass nor a red — the command genuinely succeeded."""
        assert EnumEvidenceCheckStatus.NON_PROBATIVE.value == "non_probative"

    def test_a_surrogate_does_not_count_toward_completion(self) -> None:
        """``verified_count`` is the completion tally. Provenance is not in it."""
        state = _state(
            [
                _check("real", EnumEvidenceCheckStatus.VERIFIED),
                _check("self-bind", EnumEvidenceCheckStatus.NON_PROBATIVE),
                _check("admissibility", EnumEvidenceCheckStatus.NON_PROBATIVE),
            ]
        )
        assert state.verified_count == 1
        assert state.non_probative_count == 2
        # Still in the denominator: a shortfall an operator can see is the whole
        # point. 1/3, not 1/1.
        assert state.total_checks == 3

    def test_a_contract_of_only_surrogates_never_reads_verified(self) -> None:
        """Fail toward the gap comment, never toward the flip."""
        state = _state(
            [
                _check("pr-state", EnumEvidenceCheckStatus.NON_PROBATIVE),
                _check("ci", EnumEvidenceCheckStatus.NON_PROBATIVE),
                _check("admissibility", EnumEvidenceCheckStatus.NON_PROBATIVE),
            ]
        )
        assert state.status is EnumDodVerifyStatus.SKIPPED
        assert state.verified_count == 0
        assert state.error_message is not None
        assert "NO_PROBATIVE_EVIDENCE" in state.error_message

    def test_a_real_check_still_verifies_alongside_surrogates(self) -> None:
        """The demotion subtracts a green; it must not manufacture a red."""
        state = _state(
            [
                _check("real", EnumEvidenceCheckStatus.VERIFIED),
                _check("self-bind", EnumEvidenceCheckStatus.NON_PROBATIVE),
            ]
        )
        assert state.status is EnumDodVerifyStatus.VERIFIED
        assert state.failed_count == 0

    def test_a_red_still_dominates_a_surrogate(self) -> None:
        """FAILED is unchanged and still wins. Refusal is monotone, not lossy."""
        state = _state(
            [
                _check("real", EnumEvidenceCheckStatus.FAILED),
                _check("self-bind", EnumEvidenceCheckStatus.NON_PROBATIVE),
            ]
        )
        assert state.status is EnumDodVerifyStatus.FAILED

    def test_supersession_by_a_surrogate_carrier_cannot_launder_a_green(self) -> None:
        """A retired item whose only carrier is provenance is not a completion.

        Before this ticket the carrier counted as VERIFIED, so a contract could
        read green on a supersession carried entirely by a bare ``gh pr view``.
        """
        state = _state(
            [
                _check("retired", EnumEvidenceCheckStatus.SUPERSEDED),
                _check("carrier", EnumEvidenceCheckStatus.NON_PROBATIVE),
            ]
        )
        assert state.status is not EnumDodVerifyStatus.VERIFIED
        assert state.verified_count == 0


# --------------------------------------------------------------------------
# End-to-end through the real collector, on a verbatim corpus contract
# --------------------------------------------------------------------------


def _stub_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every evidence subprocess exit 0 without leaving the machine.

    The surrogate commands in the corpus genuinely exit 0 against live GitHub —
    that is the entire complaint about them — so stubbing to 0 reproduces the
    real green rather than inventing one, and keeps the test hermetic.
    """
    from omnimarket.nodes.node_dod_verify.services import evidence_collector

    class _Completed:
        returncode = 0
        stdout = '{"number":1,"state":"MERGED"}'
        stderr = ""

    real_run = evidence_collector.subprocess.run

    def _run(*args: Any, **kwargs: Any) -> Any:
        # ``evidence_collector.subprocess`` IS the stdlib module, so this
        # rebinding is global for the test. ``git`` is passed through so the
        # receipt builder's own ``git rev-parse`` still returns a SHA rather
        # than this stub's PR JSON.
        argv = args[0] if args else kwargs.get("args")
        if isinstance(argv, list) and argv and argv[0] == "git":
            return real_run(*args, **kwargs)
        return _Completed()

    monkeypatch.setattr(evidence_collector.subprocess, "run", _run)
    # The OMN-14207 live-PR-state legs reach GitHub through their own effect
    # handler, not through the collector's subprocess. Stub them GREEN — the
    # strictly harder case, and the one that matters: a passing live-state leg
    # is exactly what would otherwise keep an all-surrogate contract reading
    # VERIFIED after its declared checks were demoted.
    monkeypatch.setattr(
        evidence_collector.EvidenceCollector,
        "_verify_live_pr",
        lambda _self, repo, pr_number: (True, f"{repo}#{pr_number}: MERGED (stub)"),
    )


def _contract_from_pin(ticket: str) -> dict[str, Any]:
    """Rebuild a minimal contract from the pinned verbatim check_values."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": ticket,
        "title": f"pinned copy of contracts/{ticket}.yaml",
        "dod_evidence": [
            {
                "id": f"{evidence_id}-{index}",
                "description": f"pinned item {index}",
                "source": "generated",
                "checks": [{"check_type": "command", "check_value": check_value}],
            }
            for index, (evidence_id, check_value, _) in enumerate(
                NAMED_VALIDATION_WAVE_CONTRACTS[ticket]
            )
        ],
    }


class TestThroughTheRealCollector:
    """The refusal holds on the executed path, not just on injected results."""

    def test_a_contract_whose_every_check_is_a_surrogate_does_not_verify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OMN-16667, verbatim: five checks, none of them able to go red.

        RED before the fix, measured on this exact contract with subprocesses
        stubbed to the exit status the live commands really return:
        ``status=failed verified=5/9`` — all five surrogates counted as
        completion. After the fix none of them does.

        The commands ARE still executed (stubbed here to exit 0 rather than
        reaching GitHub). Execute-then-demote is the deliberate choice: only a
        VERIFIED result is reclassified, so the change can subtract a green and
        can never manufacture one. Short-circuiting would have been cheaper and
        would have converted a genuine red — a PR the token cannot see — into a
        non-verdict, which is a loosening.
        """
        _stub_exit_zero(monkeypatch)

        path = tmp_path / "OMN-16667.yaml"
        path.write_text(yaml.safe_dump(_contract_from_pin("OMN-16667")))

        command = ModelDodVerifyStartCommand(
            correlation_id=uuid.uuid4(),
            ticket_id="OMN-16667",
            contract_path=str(path),
            requested_at=datetime.now(tz=UTC),
        )
        result = HandlerDodVerify()._handle_typed(command)
        assert isinstance(result, ModelDodVerifyState)

        assert result.verified_count == 0
        # 5 declared surrogate checks + 4 OMN-14207 live-PR-state legs, all of
        # which the stub returns GREEN. Every one is demoted: without demoting
        # the legs the contract would still have read VERIFIED on them alone,
        # which is precisely the hole this closes.
        assert result.non_probative_count == 9
        assert result.status is EnumDodVerifyStatus.SKIPPED
        assert result.error_message is not None
        assert "NO_PROBATIVE_EVIDENCE" in result.error_message
        for check in result.checks:
            assert check.status is EnumEvidenceCheckStatus.NON_PROBATIVE
            assert check.message is not None
            assert "NON_PROBATIVE[" in check.message

    def test_the_cli_receipt_fails_closed_on_an_all_surrogate_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable artifact, not just the in-memory state.

        ``_build_receipt`` writes ``status: PASS`` only for a VERIFIED run, and
        the completion guard reads that field. An all-surrogate contract must
        receipt FAIL and name the reason.
        """
        from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus

        from omnimarket.nodes.node_dod_verify import __main__ as dod_main

        _stub_exit_zero(monkeypatch)

        path = tmp_path / "OMN-16620.yaml"
        path.write_text(yaml.safe_dump(_contract_from_pin("OMN-16620")))

        command = ModelDodVerifyStartCommand(
            correlation_id=uuid.uuid4(),
            ticket_id="OMN-16620",
            contract_path=str(path),
            requested_at=datetime.now(tz=UTC),
        )
        state = HandlerDodVerify()._handle_typed(command)
        assert isinstance(state, ModelDodVerifyState)

        receipt = dod_main._build_receipt(
            state=state, contract_path=str(path), working_dir=tmp_path
        )
        assert receipt["status"] == EnumReceiptStatus.FAIL.value
        assert "non_probative" in str(receipt["probe_stdout"])

    def test_a_mixed_contract_still_verifies_its_real_evidence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative control on the executed path.

        One real command beside four surrogates: the run verifies, the
        surrogates are reported and not counted, and the real check is the one
        that carried the verdict.
        """
        _stub_exit_zero(monkeypatch)
        contract = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-15391",
            "title": "mixed",
            "dod_evidence": [
                {
                    "id": "dod-real",
                    "description": "a command whose exit status can move",
                    "source": "manual",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                },
                *[
                    {
                        "id": f"surrogate-{index}",
                        "description": "provenance",
                        "source": "generated",
                        "checks": [
                            {"check_type": "command", "check_value": check_value}
                        ],
                    }
                    for index, (_, check_value, _) in enumerate(
                        NAMED_VALIDATION_WAVE_CONTRACTS["OMN-16667"][:4]
                    )
                ],
            ],
        }
        path = tmp_path / "OMN-15391.yaml"
        path.write_text(yaml.safe_dump(contract))

        command = ModelDodVerifyStartCommand(
            correlation_id=uuid.uuid4(),
            ticket_id="OMN-15391",
            contract_path=str(path),
            requested_at=datetime.now(tz=UTC),
        )
        result = HandlerDodVerify()._handle_typed(command)
        assert isinstance(result, ModelDodVerifyState)

        assert result.status is EnumDodVerifyStatus.VERIFIED
        assert result.verified_count == 1
        # 4 declared surrogates + 3 live-PR-state legs derived from the PR
        # numbers their commands pin.
        assert result.non_probative_count == 7
        assert result.total_checks == 8

    def test_a_surrogate_spelled_with_the_command_key_is_still_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CodeRabbit (PR #2168): classify the EFFECTIVE command, not one key.

        ``_run_command_check`` resolves ``check.get("command") or
        check.get("check_value")``. Classifying only ``check_value`` would let a
        check spelled with the ``command`` key EXECUTE as a surrogate while
        being counted as probative — its green would still reach
        ``verified_count``, which is a complete bypass of this refusal. Zero
        live instances of this spelling exist in the OCC corpus at the pinned
        SHA, so this closes a latent hole rather than an active one.
        """
        _stub_exit_zero(monkeypatch)
        contract = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-15391",
            "title": "command-key spelling",
            "dod_evidence": [
                {
                    "id": "command-key-surrogate",
                    "description": "spelled with the command key",
                    "source": "generated",
                    "checks": [
                        {
                            "check_type": "command",
                            "command": (
                                "gh pr view 1 --repo OmniNode-ai/omnimarket "
                                "--json number,state"
                            ),
                        }
                    ],
                }
            ],
        }
        path = tmp_path / "OMN-15391.yaml"
        path.write_text(yaml.safe_dump(contract))

        command = ModelDodVerifyStartCommand(
            correlation_id=uuid.uuid4(),
            ticket_id="OMN-15391",
            contract_path=str(path),
            requested_at=datetime.now(tz=UTC),
        )
        result = HandlerDodVerify()._handle_typed(command)
        assert isinstance(result, ModelDodVerifyState)

        assert result.verified_count == 0
        assert result.non_probative_count >= 1
        assert result.status is EnumDodVerifyStatus.SKIPPED

    def test_a_surrogate_that_actually_goes_red_still_fails_the_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Monotonicity, proven on the executed path rather than asserted.

        The refusal only ever reclassifies a VERIFIED result. A surrogate whose
        command genuinely exits non-zero — the PR was deleted, the token cannot
        see the repo — is left FAILED, so nothing that is red today can go green
        because of this ticket. This is why the checks are still executed
        instead of short-circuited on classification.
        """
        from omnimarket.nodes.node_dod_verify.services import evidence_collector

        class _Failed:
            returncode = 1
            stdout = ""
            stderr = "could not resolve to a PullRequest"

        monkeypatch.setattr(
            evidence_collector.subprocess, "run", lambda *_a, **_k: _Failed()
        )

        path = tmp_path / "OMN-16667.yaml"
        path.write_text(yaml.safe_dump(_contract_from_pin("OMN-16667")))

        command = ModelDodVerifyStartCommand(
            correlation_id=uuid.uuid4(),
            ticket_id="OMN-16667",
            contract_path=str(path),
            requested_at=datetime.now(tz=UTC),
        )
        result = HandlerDodVerify()._handle_typed(command)
        assert isinstance(result, ModelDodVerifyState)

        assert result.status is EnumDodVerifyStatus.FAILED
        assert result.failed_count > 0
        assert result.non_probative_count == 0
