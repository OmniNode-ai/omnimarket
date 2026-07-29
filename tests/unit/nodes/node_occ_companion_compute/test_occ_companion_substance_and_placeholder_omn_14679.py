# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14679: the RSD companion producer must mint contracts that clear BOTH the
onex_change_control pre-commit gates that occ-preflight does NOT cover:

  * ``lint-contract-check-values`` (OMN-9350 / OMN-14673): every ``gh pr
    view|checks|diff`` ``check_value`` must be in canonical placeholder-var form
    (``${PR_NUMBER}`` / ``${REPO}``), never a hardcoded live integer.
  * ``check_contract_substance_floor`` (OMN-14409): a contract may NOT consist
    entirely of existence probes (``gh pr view --json <metadata-only>`` = tier
    L0). At least one dod_evidence check must be substantive (L1+).

Proven live on onex_change_control#4284 (25 pass / 2 fail): the un-normalized,
existence-only compute contract passed occ-preflight but FAILED both gates above.
These regressions drive the ACTUAL seam ``compute_companion_plan(request) ->
minted contract file`` and re-assert the two gates' load-bearing rules against
the minted bytes, with a RED control proving the pre-14679 form fails. The
canonical gate logic they mirror lives in onex_change_control
(``scripts/lint_contract_check_values.py`` and
``scripts/validation/check_contract_substance_floor.py``) and is not importable
here; the regexes below are copied verbatim from those gates so drift goes RED.
"""

from __future__ import annotations

import re

import pytest
import yaml

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)

# --- verbatim from scripts/lint_contract_check_values.py (OMN-9350/14673) ------
_GH_PR_PREFIX = ("gh pr checks", "gh pr view", "gh pr diff")
_HARDCODED_PR_NUMBER_RE = re.compile(r"gh pr (?:checks|view|diff)\s+\d+\s")
_BRACE_PR_RE = re.compile(r"\{pr\}")
_BRACE_REPO_RE = re.compile(r"\{repo\}")


def _lint_legacy_gh_pr(value: str) -> str | None:
    stripped = value.strip()
    if not stripped.startswith(_GH_PR_PREFIX):
        return None
    if _HARDCODED_PR_NUMBER_RE.search(stripped):
        return "hardcoded integer PR number"
    if _BRACE_PR_RE.search(stripped):
        return "wrong-format {pr} placeholder"
    if _BRACE_REPO_RE.search(stripped):
        return "wrong-format {repo} placeholder"
    if "${PR_NUMBER}" not in stripped:
        return "missing ${PR_NUMBER} placeholder"
    if "${REPO}" not in stripped and "--repo" not in stripped:
        return "missing --repo argument"
    return None


# --- verbatim from scripts/validation/check_contract_substance_floor.py --------
# (OMN-14409). An existence probe is a `gh pr view` requesting only metadata
# fields; the substantive families include a `| grep` static assertion (L1).
_EXISTENCE_JSON_FIELDS = frozenset(
    {
        "number",
        "state",
        "url",
        "title",
        "body",
        "headrefname",
        "headrefoid",
        "baserefname",
        "author",
        "isdraft",
        "mergedat",
        "createdat",
        "updatedat",
        "closedat",
        "mergeable",
        "mergestatestatus",
        "mergecommit",
    }
)
_GH_PR_VIEW_RE = re.compile(r"\bgh\s+pr\s+view\b")
_JSON_FLAG_RE = re.compile(r"--json[=\s]+([A-Za-z0-9_,]+)")
_CMD = r"(?:^|[|;&]\s*|\$\(\s*|\b(?:run|exec|xargs|sudo|time|env|then|do|else)\s+)"
_STATIC_ASSERT_RE = re.compile(rf"{_CMD}(grep|rg|ast-grep)\b")
# OMN-14783 F-06: `gh pr view --json files` names the files the PR touches — a
# DIFF ASSERTION, falsifiable about the change, so the canonical substance floor
# derives it to L1 (`_DIFF_ASSERT_RE`, check_contract_substance_floor.py:200/318).
# This branch was MISSING from this local mirror, which only recognized `| grep`;
# the omission was harmless while the compute contract's diff-scope check used
# `gh pr diff ... | grep -q .`, but the F-06 swap to `--json files` (parity with
# the born-path emitter) surfaced it. Copied verbatim so drift stays RED.
_DIFF_ASSERT_RE = re.compile(r"(--json[=\s]+[^|]*\bfiles\b|\.files\[)")


def _is_existence_probe(command: str) -> bool:
    if not _GH_PR_VIEW_RE.search(command):
        return False
    fields: set[str] = set()
    for match in _JSON_FLAG_RE.finditer(command):
        fields.update(f.strip().lower() for f in match.group(1).split(",") if f.strip())
    if not fields:
        return True
    return fields.issubset(_EXISTENCE_JSON_FIELDS)


def _is_substantive(check_value: str) -> bool:
    """True when the check derives to L1+ under the substance floor.

    Mirrors the canonical deriver's substantive families the producer emits: a
    diff-assert (`gh pr view --json files`, OMN-14783 F-06) OR a static-assert
    (`| grep`); an existence probe returns False.
    """
    command = (check_value or "").strip()
    if _is_existence_probe(command):
        return False
    return bool(_DIFF_ASSERT_RE.search(command) or _STATIC_ASSERT_RE.search(command))


def _probe() -> ModelObservedProbe:
    return ModelObservedProbe(
        command="gh pr view 321 --repo OmniNode-ai/omnimarket --json number,state",
        stdout='{"number":321,"state":"OPEN"}',
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 321,
        "pr_head_sha": "b" * 40,
        "pr_title": "feat(OMN-9999): the thing",
        "pr_body": "Implements the thing.",
        "run_timestamp": "2026-07-10T00:00:00Z",
        "product_probe": _probe(),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


def _minted_contract_checks(**request_overrides: object) -> list[str]:
    plan = compute_companion_plan(_request(**request_overrides))
    contract = next(
        f for f in plan.companion_files if f.kind == EnumCompanionFileKind.CONTRACT
    )
    data = yaml.safe_load(contract.content)
    return [ck["check_value"] for item in data["dod_evidence"] for ck in item["checks"]]


def _minted_contract_checks_by_item(
    **request_overrides: object,
) -> list[tuple[str, str]]:
    """Same as :func:`_minted_contract_checks` but pairs each value with its
    owning item id (OMN-15382: the self-bind item is a deliberate,
    id-scoped exception to the placeholder-only rule — see
    ``TestMintedContractClearsPlaceholderLint``).
    """
    plan = compute_companion_plan(_request(**request_overrides))
    contract = next(
        f for f in plan.companion_files if f.kind == EnumCompanionFileKind.CONTRACT
    )
    data = yaml.safe_load(contract.content)
    return [
        (str(item.get("id", "")), ck["check_value"])
        for item in data["dod_evidence"]
        for ck in item["checks"]
    ]


# A request whose OCC PR is known -> the contract ALSO declares the self-bind
# item, exercising every check_value the producer can mint.
_PASS2 = {
    "occ_pr_number": 4284,
    "occ_head_sha": "c" * 40,
    "occ_repo": "OmniNode-ai/onex_change_control",
    "occ_probe": ModelObservedProbe(
        command="gh pr view 4284 --repo OmniNode-ai/onex_change_control --json number,state",
        stdout='{"number":4284,"state":"OPEN"}',
        exit_code=0,
    ),
}


_ITEM_ID_PR_RE = re.compile(r"-pr-(\d+)")


@pytest.mark.unit
class TestMintedContractClearsPlaceholderLint:
    def test_pass1_every_check_value_is_placeholder_normalized(self) -> None:
        pairs = _minted_contract_checks_by_item()
        assert pairs
        for item_id, cv in pairs:
            id_pr_match = _ITEM_ID_PR_RE.search(item_id)
            if id_pr_match:
                # OMN-15382/OMN-15407 (F1x follow-up): ANY item whose id embeds
                # a PR number -- downstream, CI, self-bind -- now REQUIRES a
                # literal pin under the live lint's Rule B
                # (.onex_ratchets/omn_15382_rule_b_baseline.yaml,
                # onex_change_control@06d4294e). This local ``_lint_legacy_gh_pr``
                # mirror predates Rule A/B and unconditionally rejects any
                # hardcoded PR number, so it is intentionally not applied to
                # these items; a standalone hardcoded PR + literal --repo is
                # lint-clean under the LIVE gate's Rule A (OMN-14431).
                assert id_pr_match.group(1) in cv, (
                    f"item {item_id!r} embeds PR #{id_pr_match.group(1)} but "
                    f"check_value pins a different number: {cv!r}"
                )
                continue
            assert _lint_legacy_gh_pr(cv) is None, (
                f"lint-contract-check-values rejects: {cv}"
            )

    def test_pass2_every_check_value_is_placeholder_normalized(self) -> None:
        pairs = _minted_contract_checks_by_item(**_PASS2)
        # downstream (view + diff) + validator + self-bind (view) = 4 checks.
        assert len(pairs) == 4
        for item_id, cv in pairs:
            id_pr_match = _ITEM_ID_PR_RE.search(item_id)
            if id_pr_match:
                # OMN-15382/OMN-15407 (F1x follow-up): see the pass1 note above
                # -- every PR-number-embedding item now requires a literal pin,
                # not just the self-bind item.
                assert id_pr_match.group(1) in cv, (
                    f"item {item_id!r} embeds PR #{id_pr_match.group(1)} but "
                    f"check_value pins a different number: {cv!r}"
                )
                continue
            assert _lint_legacy_gh_pr(cv) is None, (
                f"lint-contract-check-values rejects: {cv}"
            )


@pytest.mark.unit
class TestMintedContractClearsSubstanceFloor:
    def test_pass1_declares_a_substantive_check(self) -> None:
        assert any(_is_substantive(cv) for cv in _minted_contract_checks())

    def test_pass2_declares_a_substantive_check(self) -> None:
        assert any(_is_substantive(cv) for cv in _minted_contract_checks(**_PASS2))

    def test_pass2_still_declares_the_binding_existence_probe(self) -> None:
        # The substance floor keeps existence/binding probes valid; the fix ADDS
        # a substantive check, it does not drop the Evidence-Source binding probe.
        assert any(_is_existence_probe(cv) for cv in _minted_contract_checks(**_PASS2))


@pytest.mark.unit
class TestRedControlPre14679Form:
    """feedback_prove_red_against_exists_but_wrong: the un-normalized,
    existence-only form the producer minted BEFORE OMN-14679 must fail BOTH
    gate mirrors, proving the assertions are load-bearing (not vacuously green).
    """

    _OLD_EXISTENCE_ONLY = (
        "gh pr view 1788 --repo OmniNode-ai/omnimarket --json number,state"
    )

    def test_old_form_fails_placeholder_lint(self) -> None:
        assert (
            _lint_legacy_gh_pr(self._OLD_EXISTENCE_ONLY)
            == "hardcoded integer PR number"
        )

    def test_old_form_is_not_substantive(self) -> None:
        assert not _is_substantive(self._OLD_EXISTENCE_ONLY)
