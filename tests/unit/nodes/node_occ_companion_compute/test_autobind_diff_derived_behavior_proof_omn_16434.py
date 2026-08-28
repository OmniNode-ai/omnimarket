# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16434: the OCC autobind producer must mint DIFF-DERIVED behavior proof.

Defect this pins (measured, wave-2 2026-08-28 evidence comment on OMN-16434):
``node_occ_companion_compute`` minted a FIXED surrogate corpus onto every
companion it authored — bare ``gh pr view`` PR-state probes, a
``--json files`` diff-scope read, and, unconditionally,
``uv run pytest tests/test_evidence_admissibility.py -q``, which is OCC's own
predicate suite and sits on
``omnimarket.occ_evidence_probative_class.FOREIGN_SUITE_DENYLIST``. None of
those classify BEHAVIOR under the merged OMN-15911 classifier
(``node_dod_verify.services.check_proof_class``), so EVERY autobound contract
was born at ``behavior_proving_count == 0`` and could never satisfy the
OMN-15911 autoclose flip conjunct without a hand-authored follow-up OCC PR.
Ten contracts were measured at 0; the treadmill of hand repair lost the race at
least once (OCC#7357 auto-merged before it could be corrected).

Proof structure (feedback_prove_red_against_exists_but_wrong):
  * GREEN — a PR whose diff ADDS/CHANGES a test file yields a contract carrying
    a BEHAVIOR-class check bound to that exact test file, with the product
    repo's ``cwd``, and the denylisted foreign suite is GONE.
  * RED control — the same producer run on the same PR with the diff's test
    files removed from ``changed_files`` mints NO behavior check: the contract
    stays at ``behavior_proving_count == 0``. That contrast is what proves the
    new check is derived from the diff rather than minted from a fixed corpus.
  * Honesty leg — on that no-test-change PR the producer records what proof is
    OWED in ``evidence_requirements`` instead of a fake green behavior check.
"""

from __future__ import annotations

import pytest
import yaml

from omnimarket.enums.enum_check_proof_class import EnumCheckProofClass
from omnimarket.nodes.node_dod_verify.services.check_proof_class import (
    classify_item_checks,
)
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    BEHAVIOR_PROOF_EVIDENCE_ID,
    behavior_proof_cwd,
    derive_behavior_test_paths,
)
from omnimarket.occ_evidence_probative_class import is_surrogate_check_value

_TICKET = "OMN-16434"
_REPO = "OmniNode-ai/omnibase_infra"
_PR = 2955
_HEAD = "a1a2a3a4a5a6a7a8a9a0b1b2b3b4b5b6b7b8b9b0"
_SRC_FILE = "src/omnibase_infra/services/thing.py"
_TEST_FILE = "tests/unit/services/test_thing_omn16434.py"


def _probe(pr: int) -> ModelObservedProbe:
    return ModelObservedProbe(
        command=f"gh pr view {pr}",
        stdout=f'{{"number":{pr},"state":"OPEN"}}',
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": _REPO,
        "pr_number": _PR,
        "pr_head_sha": _HEAD,
        "pr_title": f"fix({_TICKET}): producer mints diff-derived behavior proof",
        "pr_body": f"Closes {_TICKET}",
        "run_timestamp": "2026-08-28T00:00:00Z",
        "product_probe": _probe(_PR),
        "changed_files": (_SRC_FILE, _TEST_FILE),
        "diff_total_lines": 40,
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


def _contract(plan: ModelOccCompanionPlan) -> dict[str, object]:
    raw = next(
        f.content
        for f in plan.companion_files
        if f.kind == EnumCompanionFileKind.CONTRACT
    )
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


def _items(contract: dict[str, object]) -> list[dict[str, object]]:
    dod = contract.get("dod_evidence")
    assert isinstance(dod, list)
    return [item for item in dod if isinstance(item, dict)]


def _behavior_proving_count(contract: dict[str, object]) -> int:
    """The OMN-15911 conjunct, computed exactly as node_dod_verify computes it."""
    return sum(
        1
        for item in _items(contract)
        if classify_item_checks(item.get("checks") or [])
        is EnumCheckProofClass.BEHAVIOR
    )


def _check_values(contract: dict[str, object]) -> list[str]:
    values: list[str] = []
    for item in _items(contract):
        checks = item.get("checks")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if isinstance(check, dict) and isinstance(check.get("check_value"), str):
                values.append(str(check["check_value"]))
    return values


# ---------------------------------------------------------------------------
# The pure derivation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeriveBehaviorTestPaths:
    @pytest.mark.parametrize(
        ("changed", "expected"),
        [
            ((_SRC_FILE, _TEST_FILE), (_TEST_FILE,)),
            ((_SRC_FILE,), ()),
            ((), ()),
            (
                ("tests/unit/a/test_b.py", "tests/unit/a/c_test.py"),
                ("tests/unit/a/c_test.py", "tests/unit/a/test_b.py"),
            ),
            # Not a test file merely because it lives under tests/.
            (("tests/conftest.py", "tests/fixtures/data.yaml"), ()),
            # Non-Python test-named files are not pytest targets.
            (("tests/test_thing.ts",), ()),
        ],
    )
    def test_derivation_is_pure_and_diff_scoped(
        self, changed: tuple[str, ...], expected: tuple[str, ...]
    ) -> None:
        assert derive_behavior_test_paths(changed) == expected

    def test_derivation_is_bounded_and_deterministic(self) -> None:
        many = tuple(f"tests/unit/test_m{i}.py" for i in range(20))
        first = derive_behavior_test_paths(many)
        assert first == derive_behavior_test_paths(tuple(reversed(many)))
        assert 0 < len(first) <= 4

    def test_cwd_is_the_product_repo_not_the_occ_checkout(self) -> None:
        assert behavior_proof_cwd(_REPO) == "${OMNI_HOME}/omnibase_infra"


# ---------------------------------------------------------------------------
# GREEN: a PR that changes a test file gets a behavior-class check
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiffDerivedBehaviorProofIsMinted:
    def test_behavior_proving_count_is_at_least_one(self) -> None:
        contract = _contract(compute_companion_plan(_request()))
        assert _behavior_proving_count(contract) >= 1

    def test_the_behavior_check_names_the_tests_this_pr_changed(self) -> None:
        contract = _contract(compute_companion_plan(_request()))
        item = next(
            i for i in _items(contract) if i.get("id") == BEHAVIOR_PROOF_EVIDENCE_ID
        )
        checks = item.get("checks")
        assert isinstance(checks, list)
        assert len(checks) == 1
        check = checks[0]
        assert isinstance(check, dict)
        assert check["check_value"] == f"uv run pytest {_TEST_FILE} -q"
        # Executed in the PRODUCT repo: the OCC checkout has no such path, and a
        # check that cannot resolve its target proves nothing wherever it runs.
        assert check["cwd"] == "${OMNI_HOME}/omnibase_infra"
        assert classify_item_checks(checks) is EnumCheckProofClass.BEHAVIOR

    def test_the_foreign_suite_is_not_minted(self) -> None:
        values = _check_values(_contract(compute_companion_plan(_request())))
        assert ADMISSIBILITY_VALIDATOR_CHECK_VALUE not in values
        assert not any(is_surrogate_check_value(v) for v in values if "pytest" in v)

    def test_the_behavior_item_has_a_backing_receipt(self) -> None:
        # validator_occ_merge_eligibility refuses a companion whose contract
        # declares a dod_evidence item with no PASS receipt bound to it.
        plan = compute_companion_plan(_request())
        paths = {f.path for f in plan.companion_files}
        assert (
            f"drift/dod_receipts/{_TICKET}/{BEHAVIOR_PROOF_EVIDENCE_ID}/command.yaml"
            in paths
        )


# ---------------------------------------------------------------------------
# RED control + honesty leg
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoTestChangeIsDeclaredNotFaked:
    def test_no_behavior_check_is_invented(self) -> None:
        contract = _contract(
            compute_companion_plan(_request(changed_files=(_SRC_FILE,)))
        )
        assert BEHAVIOR_PROOF_EVIDENCE_ID not in {i.get("id") for i in _items(contract)}

    def test_what_is_owed_is_recorded_in_evidence_requirements(self) -> None:
        contract = _contract(
            compute_companion_plan(_request(changed_files=(_SRC_FILE,)))
        )
        reqs = contract.get("evidence_requirements")
        assert isinstance(reqs, list)
        owed = [r for r in reqs if isinstance(r, dict) and r.get("kind") == "tests"]
        assert owed, "producer must state the behavior proof it could not derive"
        assert "OWED" in str(owed[0].get("description", ""))
        assert _SRC_FILE in str(owed[0].get("description", ""))

    def test_a_test_change_records_the_derived_command_as_the_requirement(
        self,
    ) -> None:
        contract = _contract(compute_companion_plan(_request()))
        reqs = contract.get("evidence_requirements")
        assert isinstance(reqs, list)
        commands = [str(r.get("command", "")) for r in reqs if isinstance(r, dict)]
        assert f"uv run pytest {_TEST_FILE} -q" in commands


# ---------------------------------------------------------------------------
# The content-grep must never name a symbol the diff does not carry
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoContentGrepWithoutADiffDerivedSymbol:
    def test_absent_derivation_mints_no_content_read(self) -> None:
        # ``downstream_check_value`` is the ONLY channel by which a content-bound
        # ``?ref=`` grep reaches this producer, and the read-EFFECT populates it
        # solely from RED-controlled candidates parsed out of the PR's own diff
        # hunks. With no derivation there must be no content read at all — never
        # a grep for a symbol picked without reference to the diff.
        contract = _contract(
            compute_companion_plan(_request(downstream_check_value=None))
        )
        assert not any("?ref=" in v for v in _check_values(contract))

    def test_a_derived_content_read_is_not_duplicated_across_items(self) -> None:
        derived = (
            "gh api repos/OmniNode-ai/omnibase_infra/contents/"
            f"{_SRC_FILE}?ref={_HEAD} --jq '.content' | base64 -d | "
            "grep -c 'def thing'"
        )
        contract = _contract(
            compute_companion_plan(_request(downstream_check_value=derived))
        )
        content_reads = [v for v in _check_values(contract) if "?ref=" in v]
        assert len(content_reads) == len(set(content_reads)) == 1
