# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15407 — EVERY generated dod_evidence check_value is emission-time literal.

THE INVARIANT, stated once: the OCC companion producers know the product repo,
the product PR number, and (on pass 2) their own OCC repo + PR number at
emission time. A generated ``check_value`` must therefore carry those values
LITERALLY. A bare ``${PR_NUMBER}`` / ``${REPO}`` runner placeholder is only
resolvable on ONE evaluation surface -- the OCC compliance runner, which
pre-substitutes the tokens with *its own* repo/PR -- and is unresolvable on
every other surface that reads the same contract.

TWO INDEPENDENT LIVE FAILURES ESTABLISHED THAT, both on 2026-07-29/30:

1. Rule B (``lint_contract_check_values._pr_binding_violation``,
   ``onex_change_control`` dev since ``06d4294e``) hard-fails a placeholder-only
   check_value on any item whose id embeds a PR number. OMN-15382 closed that
   for the downstream / CI / self-bind items.

2. ``dod_verify`` -- a DIFFERENT out-of-band evaluator with no ambient PR
   context -- fails CLOSED on a bare placeholder with
   ``Cannot resolve PR number for <ticket>`` (``PR_LOOKUP_FAILED``). Reproduced
   three times independently on ``dod-deploy-assessment``, which is the ONE
   generated item Rule B never governed: its id embeds no PR number, so it fell
   through the OMN-15382 fix and kept the placeholder form.

Failure (2) is why this suite scopes the invariant to EVERY generated item
rather than only the PR-numbered ones. Rule B's id-embedding scope is a lint
implementation detail, not the boundary of the defect.

THE SECOND, PAIRED DEFECT (OMN-15411). Fixing the binding is necessary but not
sufficient: once ``dod_verify`` can actually resolve and RUN the deploy check,
its ``| grep -q`` terminal stage becomes live, and ``grep -q`` exits at the
FIRST match and closes its stdin -- so a still-writing upstream dies with
SIGPIPE (exit 141) and, under the ``bash -o pipefail`` runner OMN-15382
introduced, the whole check reports a FALSE RED on genuinely-passing evidence.
The two must land together, so this suite asserts both properties. ``grep -c``
is the fix: it must read to EOF to count, so the upstream never sees EPIPE, and
it still exits 1 on zero matches, so the check stays falsifiable. It is also
fail-closed on BOTH runners -- with pipefail a failing ``gh`` fails the
pipeline; without pipefail a failing ``gh`` emits nothing, the count is 0, and
``grep -c`` exits 1.

WHAT THIS SUITE DELIBERATELY DOES NOT ASSERT: that a generated check PROVES the
product change. Most do not -- see the vocabulary note atop
``occ_evidence_stamp``. Emission-time literalness is about the check being
*resolvable and correctly targeted on every surface that reads it*, which is
OMN-14679's actual goal (no stale/wrong references) reached by construction
instead of by placeholder.
"""

from __future__ import annotations

import difflib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    DEPLOY_ASSESSMENT_EVIDENCE_ID,
    ci_check_evidence_id,
    deploy_assessment_check_value,
    render_companion_contract,
    render_compute_companion_contract,
    render_compute_receipt,
    render_deploy_assessment_dod_evidence_item,
)

# Emission-time facts. Every generated check_value must be expressible from
# these alone — that is the whole claim.
_TICKET = "OMN-15407"
_REPO = "OmniNode-ai/omnimarket"
_PR = 1963
_EVIDENCE_ID = f"dod-{_REPO.replace('/', '-')}-pr-{_PR}"
_OCC_REPO = "OmniNode-ai/onex_change_control"
_OCC_PR = 5555
_SELF_BIND_ID = f"occ-self-bind-pr-{_OCC_PR}"

#: The runner-injected token vocabulary. Their presence in a GENERATED
#: check_value is the defect this ticket closes.
_RUNNER_TOKENS = ("${PR_NUMBER}", "${REPO}", "{pr}", "{repo}")

#: Early-exit stdin consumers. As the terminal stage of a pipeline they can kill
#: a still-writing upstream with SIGPIPE under pipefail (OMN-15411).
_EARLY_EXIT_CONSUMER_RE = re.compile(
    r"\|\s*(?:grep\s+-[A-Za-z]*q|head\b|awk\s+['\"]?NR==1\s*\{\s*exit)"
)


def _compute_contract(*, pass_two: bool = True, deploy: bool = True) -> str:
    """Render the compute-oracle companion contract with EVERY optional item on.

    Pass 2 + deploy-sensitive is the maximal shape: it declares the downstream
    binding probe, the diff-scope probe, the deploy-assessment item, the
    admissibility validator, and the self-bind item. Anything less would leave
    an item unexercised, which is how ``dod-deploy-assessment`` survived
    OMN-15382 in the first place.
    """
    return render_compute_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_PR,
        evidence_id=_EVIDENCE_ID,
        self_bind_evidence_id=_SELF_BIND_ID if pass_two else None,
        occ_pr_number=_OCC_PR if pass_two else None,
        occ_repo=_OCC_REPO if pass_two else None,
        emit_deploy_assessment=deploy,
    )


def _born_contract() -> str:
    return render_companion_contract(
        ticket_id=_TICKET,
        repo=_REPO,
        pr_number=_PR,
        evidence_id=_EVIDENCE_ID,
    )


def _generated_checks(contract_text: str) -> list[tuple[str, str]]:
    """Return ``(item_id, check_value)`` for every GENERATED command check.

    ``source: generated`` is the filter: a hand-authored item in a pre-existing
    contract is not this producer's output and is out of scope.
    """
    parsed = yaml.safe_load(contract_text)
    out: list[tuple[str, str]] = []
    for item in parsed.get("dod_evidence") or []:
        if not isinstance(item, dict) or item.get("source") != "generated":
            continue
        item_id = str(item.get("id", "<unknown>"))
        for check in item.get("checks") or []:
            if not isinstance(check, dict):
                continue
            value = check.get("check_value")
            if isinstance(value, str) and value.strip():
                out.append((item_id, value))
    return out


def _all_generated_checks() -> list[tuple[str, str, str]]:
    """``(path_label, item_id, check_value)`` across BOTH producer paths."""
    rows: list[tuple[str, str, str]] = []
    for label, text in (
        ("born-path", _born_contract()),
        ("compute-pass1", _compute_contract(pass_two=False, deploy=True)),
        ("compute-pass2", _compute_contract(pass_two=True, deploy=True)),
    ):
        rows.extend(
            (label, item_id, value) for item_id, value in _generated_checks(text)
        )
    return rows


# ---------------------------------------------------------------------------
# 1. No generated check carries a runner-injected placeholder
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEveryGeneratedCheckIsEmissionTimeLiteral:
    def test_the_maximal_contract_declares_every_generated_item(self) -> None:
        """Non-vacuity floor: if the render stops emitting an item this suite
        must FAIL, not silently assert over a shorter list."""
        ids = {item_id for _, item_id, _ in _all_generated_checks()}
        assert DEPLOY_ASSESSMENT_EVIDENCE_ID in ids
        assert _EVIDENCE_ID in ids
        assert ci_check_evidence_id(_EVIDENCE_ID) in ids
        assert _SELF_BIND_ID in ids

    def test_no_generated_check_value_carries_a_runner_token(self) -> None:
        offenders = [
            (label, item_id, token, value)
            for label, item_id, value in _all_generated_checks()
            for token in _RUNNER_TOKENS
            if token in value
        ]
        assert offenders == [], (
            "generated check_value carries a runner-injected placeholder; it is "
            "resolvable ONLY on the OCC compliance runner (where it silently "
            "re-targets the companion's own repo/PR) and fails closed with "
            "PR_LOOKUP_FAILED in dod_verify (OMN-15407): " + repr(offenders)
        )

    def test_every_gh_pr_check_pins_the_emission_time_pr_and_repo(self) -> None:
        """A literal is only correct if it is the RIGHT literal.

        Stale-reference regression guard for OMN-14679: the item's own subject
        PR must appear in the command, and the repo must be spelled out.
        """
        for label, item_id, value in _all_generated_checks():
            if not re.search(r"\bgh pr (?:view|checks|diff)\b", value):
                continue
            expected_pr, expected_repo = (
                (_OCC_PR, _OCC_REPO) if item_id == _SELF_BIND_ID else (_PR, _REPO)
            )
            assert f"--repo {expected_repo}" in value, (
                f"{label}/{item_id}: no literal --repo {expected_repo}: {value!r}"
            )
            assert re.search(
                rf"\bgh pr (?:view|checks|diff)\s+{expected_pr}\b", value
            ), f"{label}/{item_id}: does not pin PR #{expected_pr}: {value!r}"


# ---------------------------------------------------------------------------
# 2. The dod_verify seam — the surface OMN-15430 actually failed on
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDodVerifyCanResolveEveryGeneratedCheck:
    """Drives the REAL resolver, with PR/repo lookup forced to fail.

    This is the OMN-15430 reproduction, not a model of it: a check that needs no
    placeholder never calls the lookup, so a lookup that cannot succeed is
    exactly the condition that separates a resolvable check from an
    unresolvable one. A surrogate assertion over the string would be vacuous
    (feedback_test_the_artifact_that_runs).
    """

    @staticmethod
    def _collector_with_no_pr_context() -> EvidenceCollector:
        collector = EvidenceCollector()
        # No ambient PR context — the state a closeout dod_verify run is in.
        collector._lookup_pr_for_ticket = lambda _ticket_id: ""  # type: ignore[method-assign]
        collector._lookup_repo_for_ticket = lambda _ticket_id: ""  # type: ignore[method-assign]
        return collector

    def test_the_forced_failure_is_real(self) -> None:
        """Prove the harness can go RED before trusting its green.

        A placeholder-bearing command MUST produce the PR_LOOKUP_FAILED error
        through this exact seam; otherwise every assertion below passes for the
        wrong reason (feedback_prove_red_against_exists_but_wrong).
        """
        collector = self._collector_with_no_pr_context()
        _, error = collector._resolve_command_placeholders(
            "gh pr diff ${PR_NUMBER} --repo ${REPO} --name-only", _TICKET
        )
        assert error is not None
        assert "Cannot resolve PR number" in error

    def test_no_generated_check_needs_ambient_pr_context(self) -> None:
        collector = self._collector_with_no_pr_context()
        failures = []
        for label, item_id, value in _all_generated_checks():
            resolved, error = collector._resolve_command_placeholders(value, _TICKET)
            if error is not None:
                failures.append((label, item_id, error))
            elif resolved != value:
                failures.append(
                    (label, item_id, f"resolver rewrote the command: {resolved!r}")
                )
        assert failures == [], (
            "generated check_value is unresolvable (or silently rewritten) by "
            "dod_verify without ambient PR context — the reproducible "
            "PR_LOOKUP_FAILED red on dod-deploy-assessment (OMN-15430 / "
            "OMN-15407): " + repr(failures)
        )


# ---------------------------------------------------------------------------
# 3. SIGPIPE safety (OMN-15411) — the paired defect the binding fix unmasks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGeneratedChecksAreSigpipeSafe:
    def test_no_generated_check_pipes_into_an_early_exit_consumer(self) -> None:
        offenders = [
            (label, item_id, value)
            for label, item_id, value in _all_generated_checks()
            if _EARLY_EXIT_CONSUMER_RE.search(value)
        ]
        assert offenders == [], (
            "generated check_value pipes a streaming producer into an "
            "early-exit consumer (grep -q / head / awk-exit). Under the "
            "`bash -o pipefail` dod_verify runner the upstream dies with "
            "SIGPIPE (141) and the check reports a FALSE RED on passing "
            "evidence (OMN-15411). Use `grep -c`, which reads to EOF and still "
            "exits 1 on zero matches: " + repr(offenders)
        )

    def test_the_deploy_check_counts_rather_than_short_circuits(self) -> None:
        value = deploy_assessment_check_value(pr_number=_PR, repo=_REPO)
        assert re.search(r"\|\s*grep\s+-[A-Za-z]*c", value), value
        assert not re.search(r"grep\s+-[A-Za-z]*q", value), value

    def test_the_deploy_check_keeps_its_three_consumer_properties(self) -> None:
        """The binding fix must not cost the item its reasons for existing.

        * ``deploy`` keyword — the product repo's required deploy-gate greps
          the cited contract's dod_evidence for it (F-05 / OMN-14742).
        * ``| grep`` — clears the OMN-14409 substance floor, so the contract is
          not all-L0 (which tier it actually derives, and why that is not L1, is
          measured in ``test_the_deploy_item_clears_the_substance_floor``).
        * no ``dod_receipts`` self-reference — the OMN-15309 predicate refuses
          a check that reads back the tree this same companion authors.
        """
        value = deploy_assessment_check_value(pr_number=_PR, repo=_REPO)
        assert "deploy" in value.lower()
        assert " | grep " in value
        assert "dod_receipts" not in value

    def test_the_declared_deploy_item_matches_the_seam_function(self) -> None:
        """One authoring home: the rendered item and the receipt's recorded
        check must come from the same function, or they drift."""
        item = yaml.safe_load(
            "dod_evidence:\n"
            + render_deploy_assessment_dod_evidence_item(repo=_REPO, pr_number=_PR)
        )["dod_evidence"][0]
        assert item["id"] == DEPLOY_ASSESSMENT_EVIDENCE_ID
        assert item["checks"][0]["check_value"] == deploy_assessment_check_value(
            pr_number=_PR, repo=_REPO
        )


# ---------------------------------------------------------------------------
# 4. Consumer-gate parity — the REAL onex_change_control gates
# ---------------------------------------------------------------------------


def _occ_module_names() -> list[str]:
    return [
        name
        for name in sys.modules
        if name == "onex_change_control" or name.startswith("onex_change_control.")
    ]


@contextmanager
def _sibling_occ_package(src: Path) -> Iterator[None]:
    """Make ``<sibling>/src`` authoritative for ``onex_change_control``, then undo it.

    OMN-15539; same defect and same reasoning as the OMN-15247 suite's copy.
    ``sys.path`` insertion is a no-op once the package name is bound in
    ``sys.modules``, so in a full-suite run this file loaded the INSTALLED
    ``onex-change-control`` wheel (0.5.1) instead of the sibling checkout and
    died on ``onex_change_control.validation.evidence_admissibility``, which the
    wheel does not ship. Passing in isolation and failing in the suite is the
    signature of that ordering dependency, not a flake.

    The eviction is scoped and reversed on exit: leaving the wheel's modules
    deleted re-imports them later as fresh class objects, which breaks
    ``isinstance`` for any consumer that captured the pre-eviction class.
    """
    saved = {name: sys.modules[name] for name in _occ_module_names()}
    for name, module in saved.items():
        origin = getattr(module, "__file__", None)
        if origin is None or not Path(origin).resolve().is_relative_to(src):
            del sys.modules[name]
    try:
        yield
    finally:
        for name in _occ_module_names():
            if name not in saved:
                del sys.modules[name]
        sys.modules.update(saved)


def _load_occ_module(relpath: str, name: str) -> ModuleType | None:
    """Import an onex_change_control gate script from a sibling checkout.

    CLAUDE.md OMN-14208: the seam has to be driven for real, not modelled. The
    golden-gate workflow clones onex_change_control so this runs in CI; locally
    it skips when the sibling checkout is absent.
    """
    root = Path(os.environ.get("OCC_REPO_DIR", "../onex_change_control")).resolve()
    target = root / relpath
    if not target.is_file():
        return None
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with _sibling_occ_package(src):
        spec.loader.exec_module(module)
    return module


@pytest.mark.unit
class TestConsumerGateParityOnTheDeployBearingContract:
    """``TestConsumerGateParity`` in the OMN-15247 suite renders the born path,
    which never declares ``dod-deploy-assessment``. That blind spot is why the
    placeholder survived every gate: no parity test ever fed a deploy-bearing
    contract to the real linter. These do."""

    @pytest.fixture(scope="class")
    def deploy_bearing_contract(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        path = tmp_path_factory.mktemp("omn-15407") / f"{_TICKET}.yaml"
        path.write_text(_compute_contract(), encoding="utf-8")
        return path

    def test_lint_contract_check_values_accepts_it(
        self, deploy_bearing_contract: Path
    ) -> None:
        module = _load_occ_module(
            "scripts/lint_contract_check_values.py", "occ_lint_check_values_15407"
        )
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        violations = module.lint_contract(deploy_bearing_contract)
        assert violations == [], f"lint-contract-check-values rejects: {violations}"

    def test_the_deploy_item_clears_the_substance_floor(
        self, deploy_bearing_contract: Path
    ) -> None:
        """The deploy item must still be SUBSTANTIVE (not an L0 existence probe).

        MEASURED, and recorded here because it contradicts the comment the
        producer carried for three tickets: this value derives **L2**, not L1.
        The floor's ``_RUNTIME_PROBE_RE`` anchors its verbs to command position
        with ``(?:^|[|;&]\\s*|...)``, and the literal ``docker`` inside the grep
        alternation ``'nodes/|handlers/|...|docker|...'`` is preceded by a ``|``
        — so a *pattern* token is read as a *command* and the item is classified
        as a live-runtime probe. It is not one: it greps a filename list.

        That over-classification is PRE-EXISTING and unchanged by OMN-15407 (the
        placeholder form carried the same alternation and also derived L2), so it
        is out of this ticket's scope and deliberately not "fixed" by reordering
        the alternation to hide it. What this test pins is the property the
        contract actually needs — the item clears ``SUBSTANCE_FLOOR`` — using the
        floor's own ``satisfies`` comparison rather than a hardcoded tier string
        that would go stale the moment the accidental match is corrected
        upstream.
        """
        module = _load_occ_module(
            "scripts/validation/check_contract_substance_floor.py",
            "occ_substance_floor_15407",
        )
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        value = deploy_assessment_check_value(pr_number=_PR, repo=_REPO)
        tier = module.derive_proof_tier("command", value)
        assert tier.satisfies(module.SUBSTANCE_FLOOR), (
            f"deploy item derives {tier.value}, below the substance floor "
            f"{module.SUBSTANCE_FLOOR.value}: {value!r}"
        )
        assert tier.value != "L0", value

    def test_token_substitution_leaves_every_generated_check_unchanged(
        self, deploy_bearing_contract: Path
    ) -> None:
        """The OCC runner's ``_substitute_tokens`` must be a NO-OP.

        This is the property that makes a literal pin surface-independent: if
        substitution can still rewrite the command, the check means different
        things on different runners — the class-3 mis-binding OMN-15382 named.
        """
        module = _load_occ_module(
            "src/onex_change_control/scripts/contract_compliance_check.py",
            "occ_compliance_check_15407",
        )
        if module is None:
            pytest.skip("onex_change_control checkout not available")
        rewritten = []
        for item_id, value in _generated_checks(
            deploy_bearing_contract.read_text(encoding="utf-8")
        ):
            # Substituted with the COMPANION's own identity, as OCC CI does.
            substituted = module._substitute_tokens(
                value,
                pr_number=_OCC_PR,
                repo=_OCC_REPO,
                ticket_id=_TICKET,
            )
            if substituted != value:
                rewritten.append((item_id, value, substituted))
        assert rewritten == [], (
            "OCC token substitution rewrote a generated check_value, so the "
            "check targets the companion instead of the product PR: " + repr(rewritten)
        )

    def test_the_red_derivability_gate_recognises_every_generated_check(
        self, deploy_bearing_contract: Path
    ) -> None:
        """The in-repo ratchet is exact-match, so its allowlist and the emitter
        must move together or the gate rejects the producer's own bytes."""
        gate = _load_repo_gate()
        unknown = []
        for item_id, value in _generated_checks(
            deploy_bearing_contract.read_text(encoding="utf-8")
        ):
            classification, reason = gate.classify_check(value)
            if classification == "unknown":
                unknown.append((item_id, value, reason))
        assert unknown == [], (
            "check_generated_checks_red_derivable rejects a shape the producer "
            "emits: " + repr(unknown)
        )


# ---------------------------------------------------------------------------
# 5. The compute receipt survives the REAL yamlfmt (F-03 / OMN-14684)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeReceiptIsYamlfmtIdempotent:
    """The `yamlfmt` red on OCC#5554, reproduced at its source.

    OCC#5554 is the companion the autobind producer minted for this ticket's own
    product PR. Its `Pre-commit` job failed on `yamlfmt` — "files were modified
    by this hook" — and the modified file was the deploy-assessment RECEIPT, on
    its `actual_output` line, NOT on the `check_value` this ticket rewrote (that
    line is 157 columns and yamlfmt leaves it alone, its last space sitting at
    column 91).

    Cause: the compute receipt template inlined `actual_output` as a plain
    double-quoted scalar while interpolating `sorted(deploy_hits)[0]`, an
    arbitrary-length repository path. Pre-existing and latent — it needs a long
    first deploy-sensitive path to fire, and the PR that finally supplied one was
    this ticket's own. The born-path receipts already routed this field through
    `render_check_value_field` under OMN-15247 R21; the compute receipt never did.

    A fold rewrites the committed receipt and restales its hash, so this is
    driven against the real binary with the real config rather than modelled.
    """

    _LONG_PATH = (
        "src/omnimarket/nodes/node_occ_companion_compute/handlers/"
        "handler_occ_companion_compute.py"
    )

    def _receipt(self, actual_output: str) -> str:
        return render_compute_receipt(
            ticket_id=_TICKET,
            evidence_id=DEPLOY_ASSESSMENT_EVIDENCE_ID,
            check_value=deploy_assessment_check_value(pr_number=_PR, repo=_REPO),
            contract_sha256="a" * 64,
            contract_entry_sha256="sha256:" + "b" * 64,
            run_timestamp="2026-07-30T00:00:00Z",
            commit_sha="c" * 40,
            runner="node_pr_lifecycle_fix_effect",
            verifier="occ-evidence-source-autobind",
            probe_command=f"gh pr view {_PR}",
            probe_stdout=f'{{"number":{_PR},"state":"OPEN"}}',
            actual_output=actual_output,
            exit_code=0,
            pr_number=_PR,
            branch="jonah/example",
        )

    @property
    def _observed_actual_output(self) -> str:
        """The exact string the compute handler builds for a deploy receipt."""
        return (
            f"PASS: deploy-scope present for {_TICKET} from {_REPO}#{_PR}"
            f" — 2 runtime/deploy-sensitive path(s), e.g. {self._LONG_PATH}."
        )

    def test_the_observed_actual_output_really_is_fold_length(self) -> None:
        """Non-vacuity: prove the input is one that WOULD fold if inlined.

        Without this, a renderer change that quietly shortened the field would
        make the yamlfmt test below pass for the wrong reason.
        """
        inlined = f'actual_output: "{self._observed_actual_output}"'
        assert len(inlined) > 100
        spaces_past_wrap = [
            col for col, ch in enumerate(inlined) if ch == " " and col > 100
        ]
        assert spaces_past_wrap, inlined

    def test_the_deploy_receipt_is_a_yamlfmt_fixpoint(self, tmp_path: Path) -> None:
        yamlfmt = shutil.which("yamlfmt")
        if yamlfmt is None:
            pytest.skip("yamlfmt binary not available (installed in the CI gate)")
        conf = _occ_yamlfmt_config()
        if conf is None:
            pytest.skip("onex_change_control .yamlfmt not available")
        target = tmp_path / "command.yaml"
        target.write_text(self._receipt(self._observed_actual_output), encoding="utf-8")
        before = target.read_text(encoding="utf-8")
        subprocess.run(
            [yamlfmt, "-conf", str(conf), str(target)],
            capture_output=True,
            check=False,
        )
        after = target.read_text(encoding="utf-8")
        assert after == before, (
            "real yamlfmt rewrote the compute deploy receipt — a fold restales "
            "contract_sha256 / contract_entry_sha256 (F-03 / OMN-14684) and is "
            "the `yamlfmt` red observed on OCC#5554:\n"
            + "".join(
                difflib.unified_diff(before.splitlines(True), after.splitlines(True))
            )
        )

    def test_a_short_actual_output_keeps_the_byte_identical_quoted_form(self) -> None:
        """No blast radius: values that already fitted must not change shape.

        `render_check_value_field` only switches to a literal block for a value
        that would fold, so every pre-existing receipt renders byte-identically.
        """
        assert 'actual_output: "PASS: short."' in self._receipt("PASS: short.")

    def test_the_folding_value_switches_to_a_literal_block(self) -> None:
        assert "actual_output: |-" in self._receipt(self._observed_actual_output)

    def test_the_receipt_still_parses_and_round_trips_the_value(self) -> None:
        """A literal block must carry the SAME string, with no trailing newline."""
        parsed = yaml.safe_load(self._receipt(self._observed_actual_output))
        assert parsed["actual_output"] == self._observed_actual_output
        assert parsed["check_value"] == deploy_assessment_check_value(
            pr_number=_PR, repo=_REPO
        )


def _occ_yamlfmt_config() -> Path | None:
    root = Path(os.environ.get("OCC_REPO_DIR", "../onex_change_control")).resolve()
    conf = root / ".yamlfmt"
    return conf if conf.is_file() else None


def _load_repo_gate() -> Any:
    """Import the in-repo RED-derivability gate by path (it lives in scripts/)."""
    target = (
        Path(__file__).resolve().parents[4]
        / "scripts"
        / "ci"
        / "check_generated_checks_red_derivable.py"
    )
    spec = importlib.util.spec_from_file_location("omn15407_red_derivable_gate", target)
    assert spec is not None, target
    assert spec.loader is not None, target
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
