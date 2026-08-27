# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15988: node_occ_companion_compute emitted a VACUOUS ``dod-deploy-assessment``
probe -- one that OMN-14443's deploy-gate falsifiability ratchet
(``omniclaude/.github/actions/deploy-gate/validate_pr_deploy_required.py::
classify_check_value``) REJECTS with "no live-surface probe in command
position" on every ticket not in the frozen grandfather snapshot (i.e. every
ticket numbered above OMN-14855, the snapshot's cutoff).

LIVE REPRODUCTION (the exact failure this suite proves fixed): omnimarket#2058
(run 31617976140, job 94185423251), 2026-08-12 --

    ##[error]deploy-gate falsifiability ratchet (OMN-14443): OMN-15833 is NOT
    in the frozen grandfather snapshot (deploy_gate_legacy_grandfather.yaml)
    and OMN-15833.yaml declares NO falsifiable deploy probe -- only vacuous
    (self-referential or non-executing) evidence. New tickets must declare a
    real live-surface probe. Vacuous checks: [dod-deploy-assessment] no
    live-surface probe in command position -- the check's exit status cannot
    depend on the state of the deployed system (commands found: ['gh',
    'grep']; expected one of: ['curl', 'docker', 'docker-compose', 'gh-api',
    'httpie', 'kafkacat', 'kcat', 'kubectl', 'mongosh', 'mysql', 'nerdctl',
    'onex', 'podman', 'psql', 'redis-cli', 'rpk', 'ssh', 'valkey-cli', 'wget']

ROOT CAUSE: ``occ_evidence_stamp.deploy_assessment_check_value`` rendered
``gh pr diff <n> --repo <r> --name-only | grep -ciE '<pattern>'``. Shell-lexed,
that command's command-position tokens are ``{gh, grep}`` -- neither is in
``classify_check_value``'s ``LIVE_PROBE_COMMANDS``, which admits only the
COMPOUND token ``gh-api`` (``gh`` immediately followed by ``api``) from the
``gh`` family. The fix switches the transport to ``gh api
repos/<repo>/pulls/<pr_number>/files --paginate --jq '.[].filename' | grep
-ciE '<pattern>'`` -- same live fact (the product PR's changed-file list),
same grep-based falsifiability, but the recognized ``gh api`` verb.

This suite drives the REAL ``classify_check_value``/``has_deploy_evidence``
from a sibling ``omniclaude`` checkout (CLAUDE.md OMN-14208: the seam must be
driven for real, not modelled) -- same pattern as ``_load_occ_module`` in
``test_occ_emitter_literal_pins_omn_15407.py`` for the ``onex_change_control``
gates. Locally it skips when the sibling checkout is absent; the golden-gate
CI clones the sibling so it runs there.

Proof structure (feedback_prove_red_against_exists_but_wrong):
  * RED — the PRE-FIX check_value shape, run through the REAL classifier,
    is rejected (reproduces the defect from current dev, before this fix).
  * GREEN — the POST-FIX ``deploy_assessment_check_value`` output, run through
    the same REAL classifier, is accepted.
  * End-to-end — the REAL ``has_deploy_evidence`` (the exact function
    ``validate_pr_deploy_gate`` calls) flips False -> True for a
    NOT-grandfathered ticket ID across the same PRE-FIX -> POST-FIX contract.
  * Mutation (does-not-weaken-the-gate) — a genuinely empty/self-referential
    check_value is STILL rejected after the fix, proving OMN-14443 was not
    softened to make this ticket pass.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    DEPLOY_ASSESSMENT_EVIDENCE_ID,
    deploy_assessment_check_value,
)

_TICKET = "OMN-15833"  # the real, live-reproduced, NOT-grandfathered ticket
_REPO = "OmniNode-ai/omnibase_infra"
_PR = 2058

# The EXACT pre-fix shape ``deploy_assessment_check_value`` rendered before
# OMN-15988 (verbatim from the git history of ``occ_evidence_stamp.py``,
# OMN-15407 revision) -- pinned here as a literal so this suite proves the RED
# leg even after the source no longer contains this string anywhere.
_PRE_FIX_CHECK_VALUE = (
    f"gh pr diff {_PR} --repo {_REPO} --name-only | "
    "grep -ciE 'nodes/|handlers/|runtime/|services/|docker|monitor_logs|deploy'"
)

# A genuinely empty/absent assessment -- the mutation OMN-14443 must still
# reject after this fix (AC 5).
_GENUINELY_EMPTY_CHECK_VALUE = ""

# The classic self-referential circular grep OMN-14443 was filed to close.
_SELF_REFERENTIAL_CHECK_VALUE = (
    "grep -q '^status: PASS$' drift/dod_receipts/"
    f"{_TICKET}/{DEPLOY_ASSESSMENT_EVIDENCE_ID}/command.yaml"
)


def _load_omniclaude_module(relpath: str, name: str) -> ModuleType | None:
    """Import an omniclaude gate script from a sibling checkout.

    CLAUDE.md OMN-14208: the seam has to be driven for real, not modelled.
    Mirrors ``_load_occ_module`` in
    ``test_occ_emitter_literal_pins_omn_15407.py`` exactly, for the sibling
    ``omniclaude`` checkout instead of ``onex_change_control``. Locally it
    skips when the sibling checkout is absent (set ``OMNICLAUDE_REPO_DIR`` to
    drive it for real, e.g. ``$OMNI_HOME/omniclaude``); a golden-gate CI job
    that clones the sibling runs it for real.
    """
    root = Path(os.environ.get("OMNICLAUDE_REPO_DIR", "../omniclaude")).resolve()
    target = root / relpath
    if not target.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, target)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deploy_gate_validator() -> ModuleType:
    """The REAL deploy-gate validator module, or skip/FAIL depending on intent.

    Fail-closed where it matters: when ``OMNICLAUDE_REPO_DIR`` is EXPLICITLY set
    the caller has declared it wants the real seam driven (the golden-gate CI
    job sets it), so an unusable checkout is a hard FAILURE, not a skip — a
    silent skip in a blocking gate is the "detection that never runs" failure
    mode CLAUDE.md rule 5 names. Only the unset, local-developer case skips.
    """
    explicit = os.environ.get("OMNICLAUDE_REPO_DIR")
    module = _load_omniclaude_module(
        ".github/actions/deploy-gate/validate_pr_deploy_required.py",
        "occ_deploy_gate_validator_15988",
    )
    if module is None:
        if explicit:
            pytest.fail(
                "OMNICLAUDE_REPO_DIR is set to "
                f"{explicit!r} but .github/actions/deploy-gate/"
                "validate_pr_deploy_required.py could not be imported from it — "
                "the OMN-15988 deploy-gate ratchet parity suite must not be "
                "skipped where it was explicitly requested."
            )
        pytest.skip("omniclaude checkout not available (set OMNICLAUDE_REPO_DIR)")
    return module


def _contract_with_check_value(check_value: str, *, ticket_id: str = _TICKET) -> str:
    """A minimal, real ``ModelTicketContract``-shaped YAML with one dod_evidence
    item carrying ``check_value`` -- exactly the shape ``iter_check_values`` /
    ``has_deploy_evidence`` parse."""
    return yaml.safe_dump(
        {
            "schema_version": "1.0.0",
            "ticket_id": ticket_id,
            "title": f"Autobind OCC evidence for {ticket_id}",
            "summary": "test fixture",
            "is_seam_ticket": False,
            "interface_change": False,
            "interfaces_touched": [],
            "dod_evidence": [
                {
                    "id": DEPLOY_ASSESSMENT_EVIDENCE_ID,
                    "description": "test fixture",
                    "source": "generated",
                    "checks": [{"check_type": "command", "check_value": check_value}],
                }
            ],
        },
        sort_keys=False,
    )


@pytest.mark.unit
class TestClassifyCheckValueRedThenGreen:
    """Drives the REAL ``classify_check_value`` -- the OMN-14443 ratchet."""

    def test_red_the_pre_fix_shape_is_rejected(
        self, deploy_gate_validator: ModuleType
    ) -> None:
        """RED: reproduces the live omnimarket#2058 failure against the REAL
        classifier, from the exact pre-fix check_value shape."""
        verdict = deploy_gate_validator.classify_check_value(_PRE_FIX_CHECK_VALUE)
        assert verdict.falsifiable is False
        assert "no live-surface probe in command position" in verdict.reason
        # The exact commands the live CI run measured.
        assert "'gh', 'grep'" in verdict.reason

    def test_green_the_post_fix_shape_is_accepted(
        self, deploy_gate_validator: ModuleType
    ) -> None:
        """GREEN: the CURRENT producer output clears the same real classifier."""
        value = deploy_assessment_check_value(pr_number=_PR, repo=_REPO)
        verdict = deploy_gate_validator.classify_check_value(value)
        assert verdict.falsifiable is True, verdict.reason
        assert "gh-api" in verdict.reason

    def test_the_fix_did_not_merely_widen_the_probe_list(
        self, deploy_gate_validator: ModuleType
    ) -> None:
        """Non-vacuity floor: prove the accepted verdict traces to the ``gh
        api`` compound token this classifier actually recognizes, not a
        change to the classifier itself."""
        assert "gh-api" in deploy_gate_validator.LIVE_PROBE_COMMANDS
        assert "gh" not in deploy_gate_validator.LIVE_PROBE_COMMANDS


@pytest.mark.unit
class TestHasDeployEvidenceEndToEnd:
    """Drives the REAL ``has_deploy_evidence`` -- what ``validate_pr_deploy_gate``
    (the actual CI entrypoint) calls per cited ticket."""

    def test_red_not_grandfathered_pre_fix_contract_fails(
        self, deploy_gate_validator: ModuleType, tmp_path: Path
    ) -> None:
        """RED: a NOT-grandfathered ticket (``_TICKET`` is far above the frozen
        snapshot's OMN-14855 cutoff) whose contract carries only the pre-fix
        check_value fails deploy-gate -- the live omnimarket#2058 outcome."""
        cpath = tmp_path / f"{_TICKET}.yaml"
        cpath.write_text(
            _contract_with_check_value(_PRE_FIX_CHECK_VALUE), encoding="utf-8"
        )
        assert (
            deploy_gate_validator.has_deploy_evidence(cpath, ticket_id=_TICKET) is False
        )

    def test_green_not_grandfathered_post_fix_contract_passes(
        self, deploy_gate_validator: ModuleType, tmp_path: Path
    ) -> None:
        """GREEN: the same NOT-grandfathered ticket, with the CURRENT producer's
        check_value, clears deploy-gate -- with ZERO hand-authored repair."""
        value = deploy_assessment_check_value(pr_number=_PR, repo=_REPO)
        cpath = tmp_path / f"{_TICKET}.yaml"
        cpath.write_text(_contract_with_check_value(value), encoding="utf-8")
        assert (
            deploy_gate_validator.has_deploy_evidence(cpath, ticket_id=_TICKET) is True
        )

    @pytest.mark.parametrize(
        "check_value",
        [_GENUINELY_EMPTY_CHECK_VALUE, _SELF_REFERENTIAL_CHECK_VALUE],
        ids=["empty", "self-referential"],
    )
    def test_mutation_a_genuinely_vacuous_assessment_still_fails(
        self, deploy_gate_validator: ModuleType, tmp_path: Path, check_value: str
    ) -> None:
        """AC 5 (does-not-weaken-the-gate): reintroducing an empty/absent or
        self-referential assessment on the SAME not-grandfathered ticket must
        still FAIL after this fix. The fix must not have widened the
        classifier or the grandfather snapshot -- only corrected this one
        producer's transport."""
        cpath = tmp_path / f"{_TICKET}.yaml"
        cpath.write_text(_contract_with_check_value(check_value), encoding="utf-8")
        assert (
            deploy_gate_validator.has_deploy_evidence(cpath, ticket_id=_TICKET) is False
        )
