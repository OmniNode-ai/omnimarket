# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the OMN-15247 static RED-derivability grammar gate.

The gate is layer 2 of OMN-15247's acceptance bar ("for every generated check,
the same check run against the PR's merge-base must return non-zero"). It is
structural: it proves a check's SHAPE is capable of RED, never that it went RED
— that is layer 1 (mint-time execution, in the emitter).

The gate is driven over the REAL producer's rendered output, not a hand-written
YAML string, so a producer change that emits an un-vetted shape fails here.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    DEPLOY_ASSESSMENT_CHECK_VALUE,
    hosted_safe_binding_check_value,
    hosted_safe_diff_scope_check_value,
    render_companion_contract,
    render_downstream_receipt,
)
from omnimarket.occ_content_probe import build_content_read_check

_SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "ci"
    / "check_generated_checks_red_derivable.py"
)


def _load_gate():
    spec = importlib.util.spec_from_file_location("red_derivable_gate", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["red_derivable_gate"] = module
    spec.loader.exec_module(module)
    return module


_GATE = _load_gate()

_CONTENT_BOUND = build_content_read_check(
    repo="OmniNode-ai/omnimarket",
    path="src/omnimarket/handlers/handler_probe.py",
    kind="class",
    symbol="HandlerContentBoundProbe",
    head_sha="b" * 40,
)

# The admissible vocabulary, imported from the producer's ONE authoring home so
# this suite can never assert a shape the producer does not emit.
_ADMISSIBLE_BINDING = hosted_safe_binding_check_value()
_ADMISSIBLE_DIFF_SCOPE = hosted_safe_diff_scope_check_value()
_ADMISSIBLE_DEPLOY_SCOPE = DEPLOY_ASSESSMENT_CHECK_VALUE


@pytest.mark.unit
class TestClassifyCheck:
    def test_a_well_formed_content_bound_check_is_accepted(self) -> None:
        classification, reason = _GATE.classify_check(_CONTENT_BOUND)
        assert classification == "content_bound"
        assert reason is None

    @pytest.mark.parametrize(
        "value",
        [
            _ADMISSIBLE_BINDING,
            _ADMISSIBLE_DIFF_SCOPE,
            _ADMISSIBLE_DEPLOY_SCOPE,
        ],
    )
    def test_the_shipped_default_forms_are_allowlisted(self, value: str) -> None:
        classification, reason = _GATE.classify_check(value)
        assert classification == "allowlisted"
        assert reason is None

    def test_the_minted_admissibility_validator_is_recognised(self) -> None:
        classification, reason = _GATE.classify_check(
            ADMISSIBILITY_VALIDATOR_CHECK_VALUE
        )
        assert classification == "admissibility_validator"
        assert reason is None

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            # The circular receipt grep -- the OMN-14766 F-16 private-repo form.
            (
                "grep -q '^status: PASS$' $CONTRACT_REPO_DIR/drift/dod_receipts/"
                "OMN-9999/dod-x/command.yaml",
                "INSIDE_OWN_DIFF",
            ),
            # OMN-15247 R21b: the VACUOUS family an intermediate revision of this
            # ticket minted and this gate briefly allowlisted as its ONLY
            # permitted vocabulary. Every one of these exits 0 against any PR on
            # GitHub that changes a file, and resolves to the OCC companion's own
            # diff on the surface it runs on. Both spellings are rejected --
            # placeholder AND literal -- because the literal form was what the
            # RECEIPT recorded as provenance.
            (
                "gh api repos/${REPO}/pulls/${PR_NUMBER}/files --paginate "
                "--jq '.[].sha' | grep -qE '^[0-9a-f]{40}$'",
                "PR-existence probe",
            ),
            (
                "gh api repos/${REPO}/pulls/${PR_NUMBER}/files --paginate "
                "--jq '.[].status' | grep -qE "
                "'^(added|modified|removed|renamed|changed|copied)$'",
                "PR-existence probe",
            ),
            (
                "gh api repos/${REPO}/pulls/${PR_NUMBER}/files --paginate "
                "--jq '.[] | select(.status) | .filename' | grep -qiE 'deploy'",
                "PR-existence probe",
            ),
            (
                "gh api repos/OmniNode-ai/omnimarket/pulls/1947/files --paginate "
                "--jq '.[].sha' | grep -qE '^[0-9a-f]{40}$'",
                "PR-existence probe",
            ),
        ],
    )
    def test_the_inadmissible_and_vacuous_forms_are_rejected_by_name(
        self, value: str, fragment: str
    ) -> None:
        classification, reason = _GATE.classify_check(value)
        assert classification == "unknown"
        assert reason is not None
        assert fragment in reason, reason

    def test_the_vacuous_family_is_rejected_for_being_vacuous_not_unrecognised(
        self,
    ) -> None:
        """The diagnosis must name the property, or the gate teaches the wrong fix.

        R21's gate rejected ``gh pr view`` with a message that told the author to
        use ``gh api repos/.../pulls/<n>/files`` instead -- i.e. it actively
        prescribed the vacuous shape. This asserts the message now explains WHY
        the shape is refused (it proves nothing) rather than merely that it is
        unrecognised, so an author cannot satisfy it by re-spelling.
        """
        _classification, reason = _GATE.classify_check(
            "gh api repos/${REPO}/pulls/${PR_NUMBER}/files --paginate "
            "--jq '.[].sha' | grep -qE '^[0-9a-f]{40}$'"
        )
        assert reason is not None
        assert "matches no known generated-check form" not in reason
        assert "zero information about the change under test" in reason
        assert "companion's own diff" in reason

    @pytest.mark.parametrize(
        ("value", "fragment"),
        [
            (f"{_CONTENT_BOUND} || true", "swallows its exit code"),
            (f"{_CONTENT_BOUND} 2>/dev/null", "swallows its exit code"),
            # Looks like a content read, but no terminal grep => not falsifiable.
            (
                "gh api repos/o/r/contents/x.py?ref=" + "b" * 40 + " --jq '.content'",
                "does not match the RED-derivable grammar",
            ),
            # Empty needle: grep -c '' matches every line, can never go RED.
            (
                "gh api repos/o/r/contents/x.py?ref="
                + "b" * 40
                + " --jq '.content' | base64 -d | grep -c ''",
                "does not match the RED-derivable grammar",
            ),
            # A placeholder ref: the compliance runner has no ${SHA} token, so
            # this would run literally and could never resolve.
            (
                "gh api repos/o/r/contents/x.py?ref=${SHA} --jq '.content' "
                "| base64 -d | grep -c 'class X'",
                "does not match the RED-derivable grammar",
            ),
            ("", "empty check_value"),
            ("true", "matches no known generated-check form"),
        ],
    )
    def test_non_falsifiable_or_unvetted_shapes_are_rejected(
        self, value: str, fragment: str
    ) -> None:
        _classification, reason = _GATE.classify_check(value)
        assert reason is not None
        assert fragment in reason


@pytest.mark.unit
class TestCheckContract:
    def test_the_real_producers_default_contract_passes(self, tmp_path: Path) -> None:
        """Driven over the REAL rendering seam, not a hand-written fixture."""
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            render_companion_contract(
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=321,
                evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
            )
        )
        assert _GATE.check_contract(contract) == []

    def test_the_real_producers_content_bound_contract_passes(
        self, tmp_path: Path
    ) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            render_companion_contract(
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=321,
                evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
                downstream_check_value=_CONTENT_BOUND,
            )
        )
        assert _GATE.check_contract(contract) == []

    def test_a_smuggled_inert_check_is_reported_with_its_item_id(
        self, tmp_path: Path
    ) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            "---\n"
            'schema_version: "1.0.0"\n'
            "dod_evidence:\n"
            '  - id: "dod-smuggled"\n'
            "    checks:\n"
            '      - check_type: "command"\n'
            '        check_value: "gh pr view 1 --repo o/r --json number || true"\n'
        )
        violations = _GATE.check_contract(contract)
        by_item = {v["item"]: v["reason"] for v in violations}
        assert "swallows its exit code" in by_item["dod-smuggled"]
        # R21b: a generated contract with no admissibility-validator item is a
        # SECOND, independent violation -- it is the born-BLOCKED condition
        # itself, so it is reported even when a per-check violation is present.
        assert "admissibility-validator" in by_item["<contract>"]

    def test_main_exits_nonzero_on_a_violation(self, tmp_path: Path) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            "---\n"
            "dod_evidence:\n"
            '  - id: "x"\n'
            "    checks:\n"
            '      - check_type: "command"\n'
            '        check_value: "true"\n'
        )
        assert _GATE.main([str(contract)]) == 1

    def test_main_exits_zero_on_a_clean_contract(self, tmp_path: Path) -> None:
        contract = tmp_path / "OMN-9999.yaml"
        contract.write_text(
            render_companion_contract(
                ticket_id="OMN-9999",
                repo="OmniNode-ai/omnimarket",
                pr_number=321,
                evidence_id="dod-OmniNode-ai-omnimarket-pr-321",
            )
        )
        assert _GATE.main([str(contract)]) == 0


# ---------------------------------------------------------------------------
# OMN-15317 — the gate is WIRED, non-vacuous, and replays live
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "occ_red_derivable"

# Recorded inputs for the committed fixtures. Real, public omnimarket refs: the
# GREEN ref is the squash commit that ADDED src/omnimarket/occ_content_probe.py
# (so `class SymbolCandidate` is present there) and the RED ref is its parent
# (where the file does not exist at all, so the probe exits non-zero).
_FIX_TICKET = "OMN-15317"
_FIX_REPO = "OmniNode-ai/omnimarket"
_FIX_PR = 1922
_FIX_EVIDENCE_ID = f"dod-OmniNode-ai-omnimarket-pr-{_FIX_PR}"
_FIX_PATH = "src/omnimarket/occ_content_probe.py"
_FIX_SYMBOL = "SymbolCandidate"
_FIX_GREEN_REF = "343202f283c3734b22f16d5d2f5af69083de5bb7"
_FIX_RED_REF = "6dfe6773bbaef0a441c93069805a367d72281819"

_FIX_GOOD_CHECK = build_content_read_check(
    repo=_FIX_REPO,
    path=_FIX_PATH,
    kind="class",
    symbol=_FIX_SYMBOL,
    head_sha=_FIX_GREEN_REF,
)
# The revert shape, live: the same check pinned at the ref where the symbol does
# NOT exist. Its GREEN leg must fail.
_FIX_INVERTED_CHECK = build_content_read_check(
    repo=_FIX_REPO,
    path=_FIX_PATH,
    kind="class",
    symbol=_FIX_SYMBOL,
    head_sha=_FIX_RED_REF,
)


def _rendered_receipt(*, check: str, green: str, red: str) -> str:
    return render_downstream_receipt(
        ticket_id=_FIX_TICKET,
        evidence_id=_FIX_EVIDENCE_ID,
        pr_number=_FIX_PR,
        repo=_FIX_REPO,
        run_timestamp="2026-07-28T00:00:00Z",
        commit_sha=green,
        branch=f"auto/omninode-ai-omnimarket-pr-{_FIX_PR}-occ-autobind",
        probe_command=check,
        probe_stdout=json.dumps(
            {"evidence_ref": green, "green_exit": 0, "red_ref": red, "red_exit": 1},
            separators=(",", ":"),
            sort_keys=True,
        ),
        exit_code=0,
        check_value=check,
        actual_output=(
            f"PASS: content-bound probe GREEN at {green}, RED at merge-base {red} "
            "(exit 1)."
        ),
    )


@pytest.mark.unit
class TestFixturesAreRealProducerBytes:
    """The gate corpus is producer output, not hand-written YAML.

    Without this, the committed fixtures could drift from what the emitter
    actually renders and the gate would enforce a shape nothing emits — a
    museum, which is the failure mode one step removed from not being wired at
    all (``feedback_test_the_artifact_that_runs``).
    """

    def test_the_positive_contract_matches_the_renderer_byte_for_byte(self) -> None:
        assert (
            _FIXTURES / "companion" / "contracts" / "OMN-15317.yaml"
        ).read_text() == render_companion_contract(
            ticket_id=_FIX_TICKET,
            repo=_FIX_REPO,
            pr_number=_FIX_PR,
            evidence_id=_FIX_EVIDENCE_ID,
            downstream_check_value=_FIX_GOOD_CHECK,
        )

    def test_the_positive_receipt_matches_the_renderer_byte_for_byte(self) -> None:
        receipt = (
            _FIXTURES
            / "companion"
            / "drift"
            / "dod_receipts"
            / _FIX_TICKET
            / _FIX_EVIDENCE_ID
            / "command.yaml"
        )
        assert receipt.read_text() == _rendered_receipt(
            check=_FIX_GOOD_CHECK, green=_FIX_GREEN_REF, red=_FIX_RED_REF
        )

    def test_the_pr_existence_negative_matches_the_renderer_byte_for_byte(
        self,
    ) -> None:
        """The revert-shaped mint the producer made under the OLD default."""
        assert (
            _FIXTURES
            / "negative"
            / "pr_existence_revert"
            / "contracts"
            / "OMN-15317.yaml"
        ).read_text() == render_companion_contract(
            ticket_id=_FIX_TICKET,
            repo=_FIX_REPO,
            pr_number=_FIX_PR,
            evidence_id=_FIX_EVIDENCE_ID,
        )

    def test_the_live_negative_matches_the_renderer_byte_for_byte(self) -> None:
        assert (
            _FIXTURES
            / "negative"
            / "live_green_leg_fails"
            / "contracts"
            / "OMN-15317.yaml"
        ).read_text() == render_companion_contract(
            ticket_id=_FIX_TICKET,
            repo=_FIX_REPO,
            pr_number=_FIX_PR,
            evidence_id=_FIX_EVIDENCE_ID,
            downstream_check_value=_FIX_INVERTED_CHECK,
        )


@pytest.mark.unit
class TestNonVacuityFloors:
    """A gate that inspects nothing must not report green (CLAUDE.md rule 5)."""

    def test_the_positive_corpus_clears_both_floors(self) -> None:
        assert (
            _GATE.main(
                [
                    "--min-checks",
                    "2",
                    "--min-content-bound",
                    "1",
                    str(_FIXTURES / "companion" / "contracts" / "OMN-15317.yaml"),
                ]
            )
            == 0
        )

    def test_no_paths_at_all_fails_the_min_checks_floor(self) -> None:
        assert _GATE.main(["--min-checks", "1"]) == 1

    def test_a_nonexistent_path_fails_the_min_checks_floor(
        self, tmp_path: Path
    ) -> None:
        """An unexpanded glob or a moved fixture dir must go RED, not silently pass."""
        assert _GATE.main(["--min-checks", "1", str(tmp_path / "nope.yaml")]) == 1

    def test_the_pr_existence_revert_fixture_fails_the_content_bound_floor(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The RED proof of the OMN-15317 flip, at gate level.

        This fixture is a real revert-shaped mint under the pre-flip default: an
        existence probe that exits 0 for any PR that exists. Every check in it is
        allowlisted, so the grammar alone passes it — the ``--min-content-bound``
        floor is what makes reverting the producer default visible to CI.
        """
        exit_code = _GATE.main(
            [
                "--min-checks",
                "2",
                "--min-content-bound",
                "1",
                str(
                    _FIXTURES
                    / "negative"
                    / "pr_existence_revert"
                    / "contracts"
                    / "OMN-15317.yaml"
                ),
            ]
        )
        assert exit_code == 1
        assert "below the --min-content-bound floor" in capsys.readouterr().out


@pytest.mark.unit
class TestLiveReplay:
    """Layer 3: both legs executed. Probe execution is faked here; the live
    transcript against real refs is in the PR body and runs in CI."""

    _CONTRACT = _FIXTURES / "companion" / "contracts" / "OMN-15317.yaml"

    def _fake_probe(
        self, monkeypatch: pytest.MonkeyPatch, exits: dict[str, int]
    ) -> list[str]:
        seen: list[str] = []

        def fake(check_value: str, *, timeout: int = 60) -> tuple[str, int]:
            seen.append(check_value)
            for ref, code in exits.items():
                if f"?ref={ref}" in check_value:
                    return ("1" if code == 0 else "0"), code
            return "", 1

        monkeypatch.setattr(_GATE, "run_probe", fake)
        return seen

    def test_green_at_pinned_ref_and_red_at_merge_base_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._fake_probe(monkeypatch, {_FIX_GREEN_REF: 0, _FIX_RED_REF: 1})
        assert (
            _GATE.main(["--live", "--min-content-bound", "1", str(self._CONTRACT)]) == 0
        )
        # Both legs actually ran, and the RED ref came from the sidecar receipt.
        assert any(f"?ref={_FIX_GREEN_REF}" in c for c in seen)
        assert any(f"?ref={_FIX_RED_REF}" in c for c in seen)

    def test_a_check_that_also_passes_at_the_merge_base_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The non-falsifiability finding — the whole point of OMN-15247."""
        self._fake_probe(monkeypatch, {_FIX_GREEN_REF: 0, _FIX_RED_REF: 0})
        assert _GATE.main(["--live", str(self._CONTRACT)]) == 1
        assert "NON-FALSIFIABLE" in capsys.readouterr().out

    def test_a_check_that_does_not_hold_at_its_pinned_ref_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Asserting only the RED leg would credit a permanently broken probe."""
        self._fake_probe(monkeypatch, {_FIX_GREEN_REF: 1, _FIX_RED_REF: 1})
        assert _GATE.main(["--live", str(self._CONTRACT)]) == 1
        assert "GREEN leg failed" in capsys.readouterr().out

    def test_the_green_leg_is_retried_once_before_failing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def flaky(check_value: str, *, timeout: int = 60) -> tuple[str, int]:
            calls.append(check_value)
            if f"?ref={_FIX_GREEN_REF}" in check_value:
                return ("", 1) if len(calls) == 1 else ("1", 0)
            return "0", 1

        monkeypatch.setattr(_GATE, "run_probe", flaky)
        assert _GATE.main(["--live", str(self._CONTRACT)]) == 0
        assert sum(1 for c in calls if f"?ref={_FIX_GREEN_REF}" in c) == 2

    def test_a_missing_sidecar_receipt_is_a_violation_not_a_skip(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """No recoverable RED ref means no evidence of falsifiability — fail closed."""
        contract_dir = tmp_path / "contracts"
        contract_dir.mkdir()
        orphan = contract_dir / "OMN-15317.yaml"
        orphan.write_text(self._CONTRACT.read_text())
        self._fake_probe(monkeypatch, {_FIX_GREEN_REF: 0, _FIX_RED_REF: 1})
        assert _GATE.main(["--live", str(orphan)]) == 1
        assert "RED ref unresolvable" in capsys.readouterr().out

    def test_an_explicit_merge_base_overrides_the_sidecar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        override = "0" * 40
        seen = self._fake_probe(monkeypatch, {_FIX_GREEN_REF: 0, override: 1})
        assert (
            _GATE.main(["--live", "--merge-base", override, str(self._CONTRACT)]) == 0
        )
        assert any(f"?ref={override}" in c for c in seen)

    def test_a_red_ref_equal_to_the_pinned_ref_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._fake_probe(monkeypatch, {_FIX_GREEN_REF: 0})
        assert (
            _GATE.main(["--live", "--merge-base", _FIX_GREEN_REF, str(self._CONTRACT)])
            == 1
        )
        assert "RED ref equals the pinned ref" in capsys.readouterr().out


@pytest.mark.unit
class TestTheGateIsActuallyInvoked:
    """OMN-15317's filing reason, asserted directly.

    Before this ticket the ONLY references to the checker were this test file in
    the workflow's pytest arg list and the script path inside a pre-commit
    ``files:`` trigger regex — neither of which executes it. These assertions go
    RED if the executing steps are removed again.
    """

    # The SCRIPT path, not the bare filename: the workflow's pytest arg list and
    # the occ-emitter-golden pre-commit entry both name the checker's TEST file,
    # whose basename contains the script's basename. Matching loosely here would
    # let the exact non-wiring OMN-15317 was filed for pass this assertion.
    _SCRIPT_INVOCATION = "scripts/ci/check_generated_checks_red_derivable.py"

    def test_the_blocking_workflow_runs_the_script_in_both_modes(self) -> None:
        workflow = yaml.safe_load(
            (
                _REPO_ROOT / ".github" / "workflows" / "occ-emitter-golden-gate.yml"
            ).read_text()
        )
        runs = [
            str(step.get("run", ""))
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if self._SCRIPT_INVOCATION in str(step.get("run", ""))
        ]
        assert len(runs) == 2, f"expected a static and a live step, got {len(runs)}"
        assert sum("--live" in r for r in runs) == 1
        assert all("--min-content-bound" in r for r in runs)
        assert all("--min-checks" in r for r in runs)

    def test_a_precommit_hook_executes_the_script(self) -> None:
        config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())
        entries = [
            str(hook.get("entry", ""))
            for repo in config["repos"]
            for hook in repo.get("hooks", [])
        ]
        executing = [e for e in entries if self._SCRIPT_INVOCATION in e]
        assert executing, "no pre-commit hook executes the checker (rule 5)"
        assert all("--min-content-bound" in e for e in executing)

    def test_the_script_exits_nonzero_as_a_real_subprocess(self) -> None:
        """Executed, not asserted: the gate command itself, over the bad fixture."""
        bad = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--min-checks",
                "2",
                "--min-content-bound",
                "1",
                str(
                    _FIXTURES
                    / "negative"
                    / "pr_existence_revert"
                    / "contracts"
                    / "OMN-15317.yaml"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert bad.returncode == 1
        assert "--min-content-bound floor" in bad.stdout

        good = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--min-checks",
                "2",
                "--min-content-bound",
                "1",
                str(_FIXTURES / "companion" / "contracts" / "OMN-15317.yaml"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert good.returncode == 0, good.stdout + good.stderr
