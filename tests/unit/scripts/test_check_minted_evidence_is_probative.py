# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15391 AC2 — the mint-side gate, and proof that it can go red.

A gate whose assertion cannot fail is the very class this ticket exists to
remove, so the gate is exercised against the producer's own
deliberately-bad fixture rather than only against a passing one.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "ci" / "check_minted_evidence_is_probative.py"
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "occ_red_derivable"


def _load_gate() -> Any:
    spec = importlib.util.spec_from_file_location("_omn15391_gate", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load_gate()


class TestProducerVerifierAgreement:
    """Assertion 1 — the two notions of "observes the product" are complements."""

    def test_the_producer_and_the_verifier_agree_on_every_minted_shape(self) -> None:
        assert GATE.check_producer_verifier_agreement() == []

    def test_the_agreement_check_inspects_every_shape_the_producer_can_mint(
        self,
    ) -> None:
        """Non-vacuity: an empty shape set would make assertion 1 trivially true."""
        shapes = GATE._mintable_check_values()
        assert len(shapes) >= 6
        # Both sides of the complement must actually occur, or the assertion
        # proves nothing about one of them.
        from omnimarket.occ_evidence_probative_class import is_surrogate_check_value

        verdicts = {is_surrogate_check_value(value) for value in shapes.values()}
        assert verdicts == {True, False}

    def test_a_new_undeclared_surrogate_shape_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failure mode the gate exists for.

        A producer that learns to mint a new exit-status-invariant shape without
        telling the verifier is the mechanism by which this class grew ~40x
        after the ticket was filed. Simulated by adding such a shape.
        """
        monkeypatch.setattr(
            GATE,
            "_mintable_check_values",
            lambda: {
                "a new silent surrogate": (
                    "gh pr view 1 --repo OmniNode-ai/omnimarket --json mergedAt"
                ),
            },
        )
        # The producer's ``?ref=`` rule says "not product-observing" and the
        # verifier says "surrogate" — those agree, so this shape alone is fine.
        assert GATE.check_producer_verifier_agreement() == []

        monkeypatch.setattr(
            GATE,
            "is_product_observing_check_value",
            lambda _check_value: True,
        )
        failures = GATE.check_producer_verifier_agreement()
        assert len(failures) == 1
        assert "DISAGREEMENT" in failures[0]


class TestNoAllSurrogateCompanion:
    """Assertion 2 — a companion that can never prove anything is refused."""

    def test_the_producer_s_own_good_fixture_passes(self) -> None:
        contracts = sorted((_FIXTURES / "companion" / "contracts").glob("*.yaml"))
        assert contracts, "fixture corpus is empty — the gate would be vacuous"
        for contract in contracts:
            assert GATE.check_contract_has_probative_evidence(contract) == []

    def test_the_producer_s_own_bad_fixture_is_rejected(self) -> None:
        """RED on an independently-authored negative, not one built for this gate.

        ``negative/pr_existence_revert`` is the OMN-15317 fixture for a producer
        reverted to the ``pr_existence`` binding. Every check in it is
        exit-status-invariant, which is exactly the class this ticket names.

        The fixture must STAY all-surrogate or this test stops exercising the
        negative path. That is enforced rather than hoped for: if the fixture
        gained a probative check the gate would return no failures and the
        ``len(failures) == 1`` assertion below would go RED, and the two
        per-class assertions pin that BOTH surrogate classes are still present.
        A fixture edit cannot quietly make this test vacuous.
        """
        contract = (
            _FIXTURES
            / "negative"
            / "pr_existence_revert"
            / "contracts"
            / "OMN-15317.yaml"
        )
        assert contract.exists()
        failures = GATE.check_contract_has_probative_evidence(contract)
        assert len(failures) == 1
        assert "ALL_SURROGATE" in failures[0]
        assert "pr_state_surrogate" in failures[0]
        assert "foreign_suite_surrogate" in failures[0]

    def test_one_probative_check_is_enough(self, tmp_path: Path) -> None:
        """Surrogates beside a real check are additive provenance, not a defect."""
        contract = tmp_path / "OMN-15391.yaml"
        contract.write_text(
            yaml.safe_dump(
                {
                    "dod_evidence": [
                        {
                            "id": "surrogate",
                            "checks": [
                                {
                                    "check_type": "command",
                                    "check_value": (
                                        "gh pr view 1 --repo o/r --json number,state"
                                    ),
                                }
                            ],
                        },
                        {
                            "id": "real",
                            "checks": [
                                {
                                    "check_type": "command",
                                    "check_value": (
                                        "gh api repos/o/r/contents/x.py?ref=abc123def "
                                        "--jq '.content' | base64 -d | grep -q foo"
                                    ),
                                }
                            ],
                        },
                    ]
                }
            )
        )
        assert GATE.check_contract_has_probative_evidence(contract) == []

    def test_a_check_spelled_with_the_command_key_is_still_classified(
        self, tmp_path: Path
    ) -> None:
        """CodeRabbit (PR #2168): classify the EFFECTIVE command, not one key.

        ``EvidenceCollector._run_command_check`` resolves ``command`` first and
        falls back to ``check_value``. A gate that read only ``check_value``
        would pass a check spelled with the ``command`` key while the runner
        executed it as a surrogate — a complete bypass. Zero live instances of
        this spelling exist in the OCC corpus today (measured at the pinned
        SHA), so this closes a latent hole rather than an active one.
        """
        contract = tmp_path / "OMN-15391.yaml"
        contract.write_text(
            yaml.safe_dump(
                {
                    "dod_evidence": [
                        {
                            "id": "command-key-surrogate",
                            "checks": [
                                {
                                    "check_type": "command",
                                    "command": (
                                        "gh pr view 1 --repo o/r --json number,state"
                                    ),
                                }
                            ],
                        }
                    ]
                }
            )
        )
        failures = GATE.check_contract_has_probative_evidence(contract)
        assert len(failures) == 1
        assert "ALL_SURROGATE" in failures[0]

    def test_a_contract_with_no_evidence_at_all_is_rejected(
        self, tmp_path: Path
    ) -> None:
        contract = tmp_path / "empty.yaml"
        contract.write_text(yaml.safe_dump({"ticket_id": "OMN-15391"}))
        failures = GATE.check_contract_has_probative_evidence(contract)
        assert len(failures) == 1
        assert "NO_EVIDENCE" in failures[0]

    @pytest.mark.parametrize(
        "items",
        [
            [{"id": "no-checks-key"}],
            [{"id": "empty-checks", "checks": []}],
            [{"id": "no-check-value", "checks": [{"check_type": "command"}]}],
            [{"id": "non-string", "checks": [{"check_value": 17}]}],
        ],
    )
    def test_items_with_no_classifiable_check_get_the_accurate_reason(
        self, tmp_path: Path, items: list[dict[str, Any]]
    ) -> None:
        """CodeRabbit (PR #2168): do not report a class that was never observed.

        These contracts still fail — a contract with no runnable check cannot
        prove completion either — but calling them ALL_SURROGATE would name a
        class nothing matched and print an empty list as its evidence, which is
        the unfalsifiable-diagnosis shape this ticket exists to remove.
        """
        contract = tmp_path / "OMN-15391.yaml"
        contract.write_text(yaml.safe_dump({"dod_evidence": items}))
        failures = GATE.check_contract_has_probative_evidence(contract)
        assert len(failures) == 1
        assert "NO_CLASSIFIABLE_CHECK" in failures[0]
        assert "ALL_SURROGATE" not in failures[0]


class TestTheGateRefusesAVacuousRun:
    """A gate that inspected nothing must fail, not pass."""

    def test_paths_that_match_no_contract_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        monkeypatch.setattr("sys.argv", ["gate", str(tmp_path)])
        assert GATE.main() == 1
        assert "VACUOUS_RUN" in capsys.readouterr().err
