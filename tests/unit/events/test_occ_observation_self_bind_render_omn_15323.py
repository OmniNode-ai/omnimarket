# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15323: the self-bind artifacts must survive the gates OCC runs on them.

Three destination-repo gates decide whether these bytes can merge, and each is
asserted here against the artifact that actually ships:

  * ``yamlfmt`` (pre-commit, and OCC CI runs ``pre-commit run --all-files``):
    fails any file it would rewrite. Proven with the REAL binary.
  * ``ModelDodReceipt``'s adversarial invariants (OMN-9786/9788): a PASS receipt
    with ``verifier == runner`` or empty ``probe_stdout`` is auto-downgraded or
    rejected. Proven by validating the rendered bytes with the real model.
  * ``check_receipt_hardening`` (OCC's honesty gate, wired into the required CI
    Summary): ``contract_entry_sha256`` must equal the recomputed per-entry hash
    of the receipt's own ``dod_evidence`` item. Proven with the real hasher over
    the real contract fixture.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    OCC_OBSERVATION_EVIDENCE_TICKET,
    insert_dod_evidence_item,
    occ_observation_evidence_item_id,
    occ_observation_self_bind_check_value,
    render_occ_observation_dod_evidence_item,
    render_occ_observation_self_bind_receipt,
)

FIXTURE_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "occ_observation_selfbind"
    / "contracts"
    / "OMN-14888.yaml"
)

# Copied from onex_change_control/.yamlfmt (read 2026-07-28), same as
# test_occ_observation_store_yamlfmt_omn_15300.py — it lives in the destination
# repo, so this test cannot read the real file.
OCC_YAMLFMT_CONFIG = """\
formatter:
  retain_line_breaks: true
  max_line_length: 100
  indent: 2
  include_document_start: true
  pad_line_comments: 2
"""

_yamlfmt = shutil.which("yamlfmt")
requires_yamlfmt = pytest.mark.skipif(
    _yamlfmt is None,
    reason="yamlfmt binary not installed; the structural invariants still run",
)

OCC_REPO = "OmniNode-ai/onex_change_control"
RECORD_COMMIT = "9f2c1b7ae4d05386c0a1f5e2d3b4c5a6978877ff"


def _record(product_repo: str = "OmniNode-ai/omnimarket") -> ModelOccObservationRecord:
    return ModelOccObservationRecord(
        product_repo=product_repo,
        product_pr_number=1931,
        head_sha="d1da60916990aa83ac4c5ddd063a1bb0f18b79da",
        policy_version="v1",
        workflow_run_id=30376361463,
        run_attempt=1,
        recorded_at="2026-07-28T16:02:51Z",
        observation=ModelOccAutoauthorObservation(
            product_repo=product_repo,
            product_pr_number=1931,
            occ_pr_number=5294,
            minted_by_node=True,
            attestation_match=True,
            occ_preflight_eligible=True,
            observed_at="2026-07-28T16:02:45+00:00",
            reason="ACCEPTED: companion byte-matches the canonical plan.",
        ),
    )


def _check_value() -> str:
    return occ_observation_self_bind_check_value(
        occ_repo=OCC_REPO, record_commit_sha=RECORD_COMMIT
    )


def _appended_contract(record: ModelOccObservationRecord) -> tuple[str, str]:
    """Return (contract text with the entry appended, evidence_item_id)."""
    item = occ_observation_evidence_item_id(record)
    text = insert_dod_evidence_item(
        FIXTURE_CONTRACT.read_text(encoding="utf-8"),
        render_occ_observation_dod_evidence_item(
            record=record, evidence_item_id=item, check_value=_check_value()
        ),
    )
    return text, item


def _receipt_text(record: ModelOccObservationRecord) -> str:
    contract_text, item = _appended_contract(record)
    return render_occ_observation_self_bind_receipt(
        evidence_ticket=OCC_OBSERVATION_EVIDENCE_TICKET,
        evidence_item_id=item,
        check_value=_check_value(),
        contract_entry_sha256=compute_contract_entry_sha256(
            yaml.safe_load(contract_text), item
        ),
        run_timestamp="2026-07-28T17:00:00Z",
        record_commit_sha=RECORD_COMMIT,
        probe_stdout=RECORD_COMMIT,
        branch="auto/occ-observation-drift-occ-observations-omninode-ai--omnimarket",
        occ_repo=OCC_REPO,
    )


def _yamlfmt_roundtrip(text: str, tmp_path: Path, name: str) -> str:
    config = tmp_path / ".yamlfmt"
    config.write_text(OCC_YAMLFMT_CONFIG, encoding="utf-8")
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    assert _yamlfmt is not None
    subprocess.run(
        [_yamlfmt, "-conf", str(config), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    return target.read_text(encoding="utf-8")


@pytest.mark.unit
class TestYamlfmtStability:
    @requires_yamlfmt
    def test_receipt_is_yamlfmt_stable(self, tmp_path: Path) -> None:
        rendered = _receipt_text(_record())
        assert _yamlfmt_roundtrip(rendered, tmp_path, "command.yaml") == rendered

    @requires_yamlfmt
    def test_appended_contract_is_yamlfmt_stable(self, tmp_path: Path) -> None:
        """The whole contract, not just the block — an append can only be
        yamlfmt-clean in the context of the file it lands in."""
        text, _ = _appended_contract(_record())
        assert _yamlfmt_roundtrip(text, tmp_path, "OMN-14888.yaml") == text

    @requires_yamlfmt
    def test_stability_holds_for_the_longest_realistic_repo_slug(
        self, tmp_path: Path
    ) -> None:
        """Line length is the failure mode yamlfmt punishes, so probe the long end.

        The rendered description embeds the product repo name; ``omnibase_infra``
        is the longest slug in the fleet today.
        """
        text, _ = _appended_contract(_record("OmniNode-ai/omnibase_infra"))
        assert _yamlfmt_roundtrip(text, tmp_path, "OMN-14888.yaml") == text

    def test_command_is_a_block_scalar_not_a_wrappable_quoted_line(self) -> None:
        """Always-on ratchet where the binary is unavailable.

        The probe carries a 40-char sha plus a repo slug; as a quoted one-liner
        it exceeds ``max_line_length: 100`` and yamlfmt refolds it. A block
        scalar is literal and never rewrapped, which is why the renderer emits
        one — regressing this silently reintroduces the OMN-15300 hook failure.
        """
        assert "check_value: |-" in _receipt_text(_record())
        block, _ = _appended_contract(_record())
        assert "        check_value: |-" in block


@pytest.mark.unit
class TestReceiptSurvivesTheRealModel:
    def test_rendered_receipt_validates_and_status_stays_pass(self) -> None:
        """Not merely parseable: PASS must SURVIVE the adversarial policy.

        ``ModelDodReceipt`` silently downgrades a self-attested PASS
        (``verifier == runner``) to ADVISORY, and the eligibility gate then
        counts it as non-PASS. Asserting the status after validation is what
        catches that, where a plain ``model_validate`` would not.
        """
        receipt = ModelDodReceipt.model_validate(
            yaml.safe_load(_receipt_text(_record()))
        )
        assert receipt.status is EnumReceiptStatus.PASS
        assert receipt.ticket_id == OCC_OBSERVATION_EVIDENCE_TICKET
        assert receipt.commit_sha == RECORD_COMMIT
        assert receipt.probe_stdout.strip() == RECORD_COMMIT
        assert receipt.runner != receipt.verifier

    def test_pr_number_is_omitted_rather_than_guessed(self) -> None:
        """The receipt is authored before the PR exists; a number would be a lie."""
        receipt = ModelDodReceipt.model_validate(
            yaml.safe_load(_receipt_text(_record()))
        )
        assert receipt.pr_number is None

    def test_entry_hash_binds_to_the_appended_contract(self) -> None:
        """OMN-14650's regression in reverse: the receipt must bind to a DECLARED item."""
        record = _record()
        contract_text, item = _appended_contract(record)
        receipt = ModelDodReceipt.model_validate(yaml.safe_load(_receipt_text(record)))
        assert receipt.evidence_item_id == item
        assert receipt.contract_entry_sha256 == compute_contract_entry_sha256(
            yaml.safe_load(contract_text), item
        )
        assert receipt.contract_sha256 is None, (
            "the legacy whole-file binding must not be minted: it goes stale on "
            "every later append to this same contract"
        )


@pytest.mark.unit
class TestInsertionMatchesTheCompanionEmitter:
    def test_insert_matches_the_companion_emitter(self) -> None:
        """Anti-drift pin for the one duplicated behaviour.

        The repo forbids importing another node's private handler package from
        ``src``, so this module carries its own copy of the OMN-14741 F-04
        structural insert. Nothing but this test stops the two from diverging.
        """
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
            OccCompanionEmitter,
        )

        record = _record()
        block = render_occ_observation_dod_evidence_item(
            record=record,
            evidence_item_id=occ_observation_evidence_item_id(record),
            check_value=_check_value(),
        )
        base = FIXTURE_CONTRACT.read_text(encoding="utf-8")
        assert insert_dod_evidence_item(base, block) == (
            OccCompanionEmitter._insert_dod_evidence_items(base, [block])
        )

    def test_insert_targets_the_dod_evidence_list_not_the_end_of_file(self) -> None:
        """A contract whose dod_evidence is not the terminal key still appends right."""
        base = '---\ndod_evidence:\n  - id: "a"\ntrailing_key: 1\n'
        out = insert_dod_evidence_item(base, '  - id: "b"\n')
        assert out == '---\ndod_evidence:\n  - id: "a"\n  - id: "b"\ntrailing_key: 1\n'

    def test_insert_rejects_a_contract_with_no_dod_evidence_block(self) -> None:
        with pytest.raises(RuntimeError, match="no block-style"):
            insert_dod_evidence_item("---\nticket_id: OMN-1\n", '  - id: "b"\n')
