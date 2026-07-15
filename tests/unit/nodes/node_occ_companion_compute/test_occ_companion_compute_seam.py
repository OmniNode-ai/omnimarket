# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam tests for the pure OCC-companion COMPUTE node (RSD-1, OMN-14285).

RSD seam-tests-FIRST: these encode the oracle contracts the COMPUTE node must
satisfy, authored before the handler. They exercise ONLY the pure function
``compute_companion_plan(request) -> plan`` (+ ``deterministic_fingerprint``) with
directly-constructed requests — zero git / gh / filesystem I/O.

  - T1 determinism: same request -> byte-identical plan; render->parse identity.
  - T2 reproducibility/attestation oracle: the deterministic digest is stable
    across requests that differ ONLY in observed facts (timestamp/probe), and a
    tampered companion file changes the digest. (This IS the OMN-14055 oracle
    property RSD-5 wires into the gate.)
  - T3 two-audiences / post-merge append: a merged-contract second consumer gets
    net-new supersede files carrying BOTH hashes; no merged receipt is mutated.
  - T6 net-new-only: every companion file is net-new (append-only gate green).
"""

from __future__ import annotations

import ast
import hashlib
import pathlib

import pytest
import yaml
from omnibase_compat.contracts.pr_occ_stamp import (
    EnumPrEvidenceSourceKind,
    parse_pr_occ_metadata_stamp,
)
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.validation.validator_occ_merge_eligibility import (
    ModelOccEligibilityInput,
    _receipt_bound_to_pr,
)

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
    deterministic_fingerprint,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)


def _probe(stdout: str = '{"number":321,"state":"OPEN"}') -> ModelObservedProbe:
    return ModelObservedProbe(
        command="gh pr view 321 --repo OmniNode-ai/omnimarket --json number,state",
        stdout=stdout,
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    base: dict[str, object] = {
        "repo": "OmniNode-ai/omnimarket",
        "pr_number": 321,
        "pr_head_sha": "b" * 40,
        "pr_title": "feat(OMN-9999): the thing",
        "pr_body": "Implements the thing.",
        "pr_state": "open",
        "pr_head_ref": "feature-branch",
        "run_timestamp": "2026-07-10T00:00:00Z",
        "product_probe": _probe(),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Request boundary validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequestBoundaryValidation:
    def test_rejects_malformed_product_head_sha(self) -> None:
        with pytest.raises(ValueError, match="7-40 hexadecimal"):
            _request(pr_head_sha="not-a-sha")

    def test_rejects_malformed_occ_head_sha_when_present(self) -> None:
        with pytest.raises(ValueError, match="7-40 hexadecimal"):
            _request(occ_pr_number=55, occ_head_sha="not-a-sha")

    def test_allows_missing_occ_head_sha_on_first_pass(self) -> None:
        assert _request(occ_head_sha=None).occ_head_sha is None

    def test_rejects_duplicate_occ_contract_state_ticket_ids(self) -> None:
        state_a = ModelOccContractState(ticket_id="OMN-9999", exists=True)
        state_b = ModelOccContractState(ticket_id="OMN-9999", exists=False)
        with pytest.raises(ValueError, match="duplicate ticket_id"):
            _request(occ_contract_states=(state_a, state_b))


# ---------------------------------------------------------------------------
# T1 — determinism
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestT1Determinism:
    def test_same_request_byte_identical_plan(self) -> None:
        req = _request()
        assert compute_companion_plan(req) == compute_companion_plan(req)

    def test_authors_a_contract_and_downstream_receipt_for_cited_ticket(self) -> None:
        plan = compute_companion_plan(_request())
        assert plan.tickets == ("OMN-9999",)
        kinds = {f.kind for f in plan.companion_files}
        assert EnumCompanionFileKind.CONTRACT in kinds
        assert EnumCompanionFileKind.DOWNSTREAM_RECEIPT in kinds
        contract = next(
            f for f in plan.companion_files if f.kind == EnumCompanionFileKind.CONTRACT
        )
        assert contract.path == "contracts/OMN-9999.yaml"
        assert 'ticket_id: "OMN-9999"' in contract.content

    def test_downstream_receipt_binds_product_head_sha(self) -> None:
        plan = compute_companion_plan(_request())
        receipt = next(
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.DOWNSTREAM_RECEIPT
        )
        assert 'commit_sha: "' + "b" * 40 + '"' in receipt.content

    def test_stamped_body_round_trips_when_occ_pr_known(self) -> None:
        plan = compute_companion_plan(_request(occ_pr_number=55, occ_head_sha="c" * 40))
        assert plan.evidence_source_occ_pr == 55
        parsed = parse_pr_occ_metadata_stamp(plan.product_body_stamped)
        assert parsed.evidence_source is not None
        assert parsed.evidence_source.kind is EnumPrEvidenceSourceKind.OCC_PR
        assert parsed.evidence_source.occ_pr_number == 55

    def test_no_ticket_is_no_op(self) -> None:
        plan = compute_companion_plan(_request(pr_title="chore: no ticket", pr_body=""))
        assert plan.no_op is True
        assert plan.companion_files == ()

    def test_already_bound_body_is_no_op(self) -> None:
        bound = "context\n\nEvidence-Source: OCC#123\nEvidence-Ticket: OMN-9999\n"
        plan = compute_companion_plan(_request(pr_body=bound))
        assert plan.no_op is True
        assert "already bound" in plan.no_op_reason.lower()


# ---------------------------------------------------------------------------
# T2 — reproducibility / attestation oracle (OMN-14055 property)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestT2ReproducibilityOracle:
    def test_digest_stable_across_observed_fact_only_changes(self) -> None:
        # Two requests identical EXCEPT the non-reproducible observed facts.
        req_a = _request(
            run_timestamp="2026-07-10T00:00:00Z", product_probe=_probe("{}")
        )
        req_b = _request(
            run_timestamp="2026-07-10T23:59:59Z",
            product_probe=_probe('{"number":321,"state":"MERGED"}'),
        )
        plan_a = compute_companion_plan(req_a)
        plan_b = compute_companion_plan(req_b)
        # Full content DIFFERS (timestamp/probe are in the receipt bytes)...
        assert plan_a.companion_files != plan_b.companion_files
        # ...but the deterministic fingerprint is identical: the oracle can
        # re-probe live GitHub and still confirm the deterministic subset.
        assert plan_a.deterministic_digest == plan_b.deterministic_digest
        assert plan_a.deterministic_digest != ""

    def test_tampered_deterministic_field_changes_digest(self) -> None:
        plan = compute_companion_plan(_request())
        # A hand-author mutates a deterministic field (the ticket binding).
        tampered = tuple(
            f.model_copy(update={"content": f.content.replace("OMN-9999", "OMN-0001")})
            for f in plan.companion_files
        )
        assert deterministic_fingerprint(tampered) != plan.deterministic_digest

    def test_plan_digest_matches_fingerprint_of_its_files(self) -> None:
        plan = compute_companion_plan(_request())
        assert plan.deterministic_digest == deterministic_fingerprint(
            plan.companion_files
        )
        # And it is a real sha256 hex.
        assert len(plan.deterministic_digest) == 64


# ---------------------------------------------------------------------------
# T3 — two-audiences / post-merge append (OMN-14233)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestT3TwoAudiences:
    def _merged_state(self) -> ModelOccContractState:
        # A contract already merged for a first consumer, with one entry.
        contract_text = (
            '---\nschema_version: "1.0.0"\nticket_id: "OMN-9999"\n'
            "dod_evidence:\n  - id: dod-omninode-ai-omnimarket-pr-100\n"
        )
        return ModelOccContractState(
            ticket_id="OMN-9999",
            exists=True,
            merged=True,
            existing_entry_ids=("dod-omninode-ai-omnimarket-pr-100",),
            whole_file_sha256=hashlib.sha256(contract_text.encode()).hexdigest(),
            raw_contract_text=contract_text,
        )

    def test_merged_second_consumer_emits_net_new_supersede(self) -> None:
        plan = compute_companion_plan(
            _request(occ_contract_states=(self._merged_state(),))
        )
        supersedes = [
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.SUPERSEDE_RECEIPT
        ]
        assert supersedes, "a merged-contract 2nd consumer must emit supersede files"
        for f in supersedes:
            # BOTH hashes carried (whole-file for hardening + per-entry for preflight).
            assert f.contract_sha256 != ""
            assert f.contract_entry_sha256 != ""
            assert f.is_net_new is True

    def test_no_merged_receipt_is_mutated(self) -> None:
        plan = compute_companion_plan(
            _request(occ_contract_states=(self._merged_state(),))
        )
        # Every emitted file is net-new; none is a rewrite of the merged receipt.
        assert all(f.is_net_new for f in plan.companion_files)
        # The already-merged entry id is never re-authored as a plain downstream
        # receipt (that would be a merged-file mutation).
        downstream = [
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.DOWNSTREAM_RECEIPT
        ]
        assert all(
            "dod-omninode-ai-omnimarket-pr-100" not in f.path for f in downstream
        )


# ---------------------------------------------------------------------------
# T6 — net-new-only
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestT6NetNewOnly:
    def test_fresh_contract_all_net_new(self) -> None:
        plan = compute_companion_plan(_request())
        assert plan.companion_files
        assert all(f.is_net_new for f in plan.companion_files)

    def test_self_bind_included_when_occ_pr_known(self) -> None:
        plan = compute_companion_plan(
            _request(
                occ_pr_number=55,
                occ_head_sha="c" * 40,
                occ_probe=_probe('{"number":55,"state":"OPEN"}'),
            )
        )
        kinds = {f.kind for f in plan.companion_files}
        assert EnumCompanionFileKind.SELF_BIND_RECEIPT in kinds
        assert all(f.is_net_new for f in plan.companion_files)


# ---------------------------------------------------------------------------
# Trivial-infra fast-path + verifier!=runner (ported honesty checks)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPortedComputeChecks:
    def test_trivial_infra_fast_path_skips_companion(self) -> None:
        plan = compute_companion_plan(
            _request(changed_files=("deploy/Dockerfile",), diff_total_lines=2)
        )
        assert plan.fast_path is True
        assert plan.companion_files == ()

    def test_runtime_edit_does_not_fast_path(self) -> None:
        plan = compute_companion_plan(
            _request(
                changed_files=("src/omnimarket/nodes/node_x/handlers/handler_x.py",),
                diff_total_lines=1,
            )
        )
        assert plan.fast_path is False
        assert plan.companion_files

    def test_verifier_equals_runner_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="self-attestation"):
            compute_companion_plan(_request(runner="same", verifier="same"))

    def test_skip_token_in_body_is_wedged(self) -> None:
        plan = compute_companion_plan(
            _request(pr_body="Implements it. [skip-deploy-gate: because reasons]")
        )
        assert any(w.code == "skip_token_present" for w in plan.wedges)


# ---------------------------------------------------------------------------
# OMN-14619 — read-EFFECT-supplied content-read check_value override
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDownstreamCheckOverride:
    """The read-EFFECT (node_occ_state_effect) may supply an honest content-read
    check_value; the COMPUTE node must use it verbatim instead of its generic
    PR-state fallback, and must keep the fallback when none is supplied
    (backward compatible with every request built before this field existed).
    """

    def test_default_falls_back_to_generic_pr_state_check(self) -> None:
        plan = compute_companion_plan(_request())
        downstream = next(
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.DOWNSTREAM_RECEIPT
        )
        parsed = yaml.safe_load(downstream.content)
        assert parsed["check_value"] == (
            "gh pr view 321 --repo OmniNode-ai/omnimarket --json number,state,headRefName"
        )

    def test_supplied_check_value_is_used_verbatim(self) -> None:
        honest_check = (
            "gh api repos/OmniNode-ai/omnimarket/contents/src/x.py?ref="
            + "b" * 40
            + " --jq -r .content | base64 -d | grep -c 'class HandlerX'"
        )
        plan = compute_companion_plan(_request(downstream_check_value=honest_check))
        downstream = next(
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.DOWNSTREAM_RECEIPT
        )
        parsed = yaml.safe_load(downstream.content)
        assert parsed["check_value"] == honest_check


# ---------------------------------------------------------------------------
# Purity guard — the COMPUTE handler must do ZERO I/O (load-bearing for the
# attestation oracle: a hidden probe/clone/now() breaks deterministic re-run).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestZeroIoPurity:
    _HANDLERS_DIR = (
        pathlib.Path(__file__).resolve().parents[4]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_occ_companion_compute"
        / "handlers"
    )
    # Both routed operations on this node (compute + the OMN-14055 attestation
    # oracle) must stay zero-I/O — the oracle re-invokes compute_companion_plan
    # in-process, so a hidden probe/clone/now() in EITHER file breaks
    # deterministic re-run.
    _HANDLER_FILES = (
        _HANDLERS_DIR / "handler_occ_companion_compute.py",
        _HANDLERS_DIR / "handler_occ_companion_attestation.py",
    )
    _BANNED_IMPORT_ROOTS = frozenset(
        {
            "subprocess",
            "requests",
            "httpx",
            "socket",
            "urllib",
            "pathlib",
            "os",
            "datetime",
            "time",
        }
    )
    _BANNED_CALL_NAMES = frozenset({"open"})
    _BANNED_CALL_ATTRS = frozenset(
        {
            "run",
            "check_output",
            "Popen",
            "clone",
            "system",
            "now",
            "today",
            "read_text",
            "write_text",
            "read_bytes",
            "write_bytes",
            "is_file",
            "exists",
        }
    )

    @pytest.mark.parametrize("handler_path", _HANDLER_FILES, ids=lambda p: p.name)
    def test_handler_module_has_no_io(self, handler_path: pathlib.Path) -> None:
        tree = ast.parse(handler_path.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in self._BANNED_IMPORT_ROOTS:
                        offenders.append(f"import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in self._BANNED_IMPORT_ROOTS:
                    offenders.append(f"from {node.module} import ...")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in self._BANNED_CALL_NAMES:
                    offenders.append(f"{func.id}()")
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr in self._BANNED_CALL_ATTRS
                ):
                    offenders.append(f".{func.attr}()")
        assert offenders == [], (
            f"{handler_path.name} must do ZERO I/O (pure COMPUTE / attestation "
            f"oracle) — found: {offenders}"
        )


# ---------------------------------------------------------------------------
# OMN-14550 — the OCC self-bind receipt binds the OCC companion PR, not the
# product PR. Pre-fix, ``_receipt`` stamped ``request.pr_number`` (the PRODUCT
# PR) into EVERY receipt including the self-bind one, so core's
# ``_receipt_bound_to_pr`` failed the ``receipt.pr_number == <OCC PR>`` branch
# and occ-preflight rejected the companion with ``pr_ticket_mismatch``.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOmn14550SelfBindPrNumber:
    _PRODUCT_PR = 321
    _OCC_PR = 55
    _OCC_HEAD = "c" * 40

    def _self_bind_receipt_text(self) -> str:
        plan = compute_companion_plan(
            _request(
                pr_number=self._PRODUCT_PR,
                occ_pr_number=self._OCC_PR,
                occ_head_sha=self._OCC_HEAD,
                occ_probe=_probe('{"number":55,"state":"OPEN"}'),
            )
        )
        receipt = next(
            f
            for f in plan.companion_files
            if f.kind == EnumCompanionFileKind.SELF_BIND_RECEIPT
        )
        return receipt.content

    def test_self_bind_pr_number_is_occ_pr_not_product_pr(self) -> None:
        # Parse the YAML and read the field programmatically — never grep raw
        # bytes (OMN-14550 fix scope #3: check_value text can contain the same
        # substring, producing a self-referential false-positive self-check).
        parsed = yaml.safe_load(self._self_bind_receipt_text())
        assert parsed["pr_number"] == self._OCC_PR
        assert parsed["pr_number"] != self._PRODUCT_PR

    def test_self_bind_check_value_targets_occ_pr_not_a_commit_sha(self) -> None:
        # Fix scope #2: the self-bind check_value asserts the durable OCC PR
        # identity (``gh pr view <OCC_PR>``), never a commit-SHA substring that
        # may not appear in the receipt or the branch's real history.
        parsed = yaml.safe_load(self._self_bind_receipt_text())
        assert f"gh pr view {self._OCC_PR}" in parsed["check_value"]
        assert str(self._PRODUCT_PR) not in parsed["check_value"]

    def test_self_bind_binds_occ_pr_through_core_validator(self) -> None:
        # Drive the ACTUAL binding seam core's occ-preflight uses. The OCC PR
        # snapshot's commit set deliberately EXCLUDES the receipt commit (a
        # rebased/squashed OCC branch), so binding can ONLY succeed via the
        # ``pr_number`` branch — isolating the OMN-14550 defect. Pre-fix
        # pr_number=321 fails both branches (pr_ticket_mismatch); post-fix
        # pr_number=55 binds by pr_number.
        receipt = ModelDodReceipt.model_validate(
            yaml.safe_load(self._self_bind_receipt_text())
        )
        snapshot = ModelOccEligibilityInput(
            repo="OmniNode-ai/onex_change_control",
            pr_number=self._OCC_PR,
            occ_commit_sha="d" * 40,
            contracts_dir=pathlib.Path("/nonexistent/contracts"),
            receipts_dir=pathlib.Path("/nonexistent/receipts"),
            pr_commit_shas=("d" * 40,),  # excludes the receipt commit on purpose
            pr_commit_texts=(),
        )
        assert receipt.commit_sha.lower() not in {
            s.lower() for s in snapshot.pr_commit_shas
        }, "test setup invariant: commit_sha must NOT match so only pr_number binds"
        assert _receipt_bound_to_pr(receipt, snapshot) is True
