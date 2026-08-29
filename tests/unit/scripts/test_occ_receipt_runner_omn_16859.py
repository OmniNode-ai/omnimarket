# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16859 AC3b — the product-repo OCC receipt runner.

What this covers, and why it is the terminal fix
------------------------------------------------
Both OCC companion producers run inside the .201 dev-lane effects runtime,
which holds no product-repo checkout: the declared ``cwd`` is
``${OMNI_HOME}/<repo>``, a path that does not exist there. So neither can ever
execute a ``test_passes`` check. The hosted OCC compliance runner DECLINES for
the same reason. Meanwhile eligibility requires a PASS receipt *before* the
companion merges, and the companion merges before the product PR. The result
for months: either a hand-authored receipt (four occurrences on 2026-08-28
alone) or a ``status: PASS`` minted behind a ``gh pr view`` probe.

The one surface that can honestly execute the check is the product repo's own
CI -- it has the checkout, the dependencies and the real test targets. It has
never had a write path into the open companion. This module tests that path.

The arrangement is only sound because of a mechanical fact verified against
``omnibase_core/.github/workflows/occ-preflight.yml``: for an OPEN companion,
eligibility pins ``occ_sha = headRefOid`` -- the companion BRANCH TIP, not OCC
main. A receipt pushed to that branch is therefore visible to the product PR's
next preflight evaluation, with no merge-ordering deadlock.

The properties under test are the ones that decide whether this is evidence or
theatre:

* **Append-only is absolute.** A merged or born receipt is never edited. The
  executed result arrives as a net-new supersession record, the primitive
  ``resolve_supersession`` already exists to serve.
* **The record must actually resolve.** ``resolve_supersession`` key-validates
  a record's own ``check_type`` against the key it is filed under; the emitter's
  supersede renderer got exactly this wrong (live: OCC#7465 filed
  ``command.supersede.2192.yaml`` for a ``test_passes`` item, so the rebind
  silently never applied). One test drives core's REAL resolver over the
  record this writer produces, so the same defect cannot ship here.
* **A failing check produces FAIL, never PASS.** Two tests execute real
  subprocesses -- a real success and a real failure -- so the executor is
  proven to be an executor and not a formatter.
* **The declared check is what runs.** Verbatim, from the companion contract.
  This also satisfies the OMN-15459 S2 family-binding rule by construction:
  the replacement's ``check_value`` IS the superseded item's declared bar, so
  the OCC#5534 rebind-to-an-unrelated-probe class is unreachable here.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.models.contracts.ticket.model_receipt_supersession import (
    ModelReceiptSupersession,
)
from omnibase_core.validation.validator_receipt_supersession import (
    resolve_supersession,
)

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import occ_receipt_runner as runner  # noqa: E402

TICKET = "OMN-16859"
ITEM = "dod-occ-diff-derived-behavior-proof"
PR_NUMBER = 2210
HEAD_SHA = "a" * 40
OTHER_SHA = "b" * 40
BRANCH = "jonah/omn-16859-occ-receipt-runner"
REPO = "OmniNode-ai/omnimarket"

# A real, fast, portable command with no absolute path in it. Using a genuine
# subprocess rather than a stub is deliberate for the PASS/FAIL pair below:
# the whole defect class this ticket exists to close is a receipt that reports
# an outcome nothing produced.
# The machine-specific absolute-path prefixes OCC's Receipt Honesty Gate
# rejects (OMN-15710 ABS_PATH). Their ABSENCE from the receipt is the
# assertion, which is why the literals appear here at all.
_ABS_PATH_PREFIXES = (
    "/Users/",  # test-literal-ok: absence is the assertion
    "/Volumes/",  # test-literal-ok: absence is the assertion
    "/home/",  # test-literal-ok: absence is the assertion
)

PASSING_CHECK = "printf '1 passed in 0.01s\\n'"
FAILING_CHECK = "printf 'E   assert False\\n'; exit 1"


def _contract(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ticket_id": TICKET,
        "title": "automatic OCC receipt generation",
        "dod_evidence": items,
    }


def _item(item_id: str, check_type: str, check_value: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "description": f"probe {item_id}",
        "source": "generated",
        "checks": [{"check_type": check_type, "check_value": check_value}],
    }


def _occ_root(tmp_path: Path, items: list[dict[str, Any]]) -> Path:
    """Build a companion checkout carrying one contract."""
    root = tmp_path / "occ"
    (root / "contracts").mkdir(parents=True, exist_ok=True)
    (root / "contracts" / f"{TICKET}.yaml").write_text(
        yaml.safe_dump(_contract(items), sort_keys=True), encoding="utf-8"
    )
    return root


def _receipt_dir(occ_root: Path, item_id: str = ITEM) -> Path:
    return occ_root / "drift" / "dod_receipts" / TICKET / item_id


def _write_born_pending(occ_root: Path, check_value: str) -> Path:
    """The honest born receipt the bridge (AC3a) mints: PENDING, no stdout."""
    path = _receipt_dir(occ_root) / "test_passes.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": TICKET,
                "evidence_item_id": ITEM,
                "check_type": "test_passes",
                "check_value": check_value,
                "status": "PENDING",
                "run_timestamp": datetime(2026, 8, 29, 10, 0, tzinfo=UTC),
                "commit_sha": HEAD_SHA,
                "runner": "OccCompanionEmitter",
                "verifier": "occ-autobind born path",
                "probe_command": check_value,
                "probe_stdout": "",
                "exit_code": None,
                "pr_number": PR_NUMBER,
                "contract_sha256": f"sha256:{'0' * 64}",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _execute(occ_root: Path, product_root: Path) -> runner.RunnerOutcome:
    return runner.run(
        occ_root=occ_root,
        product_root=product_root,
        ticket_ids=(TICKET,),
        pr_number=PR_NUMBER,
        repo=REPO,
        head_sha=HEAD_SHA,
        branch=BRANCH,
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
    )


def _load(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def _supersede_files(occ_root: Path, item_id: str = ITEM) -> list[Path]:
    directory = _receipt_dir(occ_root, item_id)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.supersede.*.yaml"))


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_born_pending_receipt_is_superseded_never_edited(tmp_path: Path) -> None:
    """The base receipt is byte-identical after the run.

    Append-only is not a preference here; it is the property the whole OCC
    evidence store rests on. The executed result must arrive as a NET-NEW file.
    """
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])
    base = _write_born_pending(occ_root, PASSING_CHECK)
    before = base.read_bytes()

    _execute(occ_root, tmp_path)

    assert base.read_bytes() == before
    assert len(_supersede_files(occ_root)) == 1


@pytest.mark.unit
def test_an_existing_supersession_record_is_never_overwritten(tmp_path: Path) -> None:
    """A second run at the same head does not rewrite the record it wrote.

    Without this the runner would quietly mutate merged evidence on every
    ``synchronize`` event -- an append-only store that appends by overwriting.
    """
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])
    _write_born_pending(occ_root, PASSING_CHECK)

    _execute(occ_root, tmp_path)
    written = _supersede_files(occ_root)[0]
    before = written.read_bytes()

    _execute(occ_root, tmp_path)

    assert written.read_bytes() == before
    assert len(_supersede_files(occ_root)) == 1


@pytest.mark.unit
def test_an_absent_receipt_is_written_as_a_net_new_base(tmp_path: Path) -> None:
    """No base receipt at all -> write one directly; no record needed.

    This is the pre-#2195 compute-producer shape, where the declared
    ``test_passes`` key had no receipt file of any kind.
    """
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])

    _execute(occ_root, tmp_path)

    base = _receipt_dir(occ_root) / "test_passes.yaml"
    assert base.is_file()
    assert _supersede_files(occ_root) == []
    assert _load(base)["status"] == "PASS"


# ---------------------------------------------------------------------------
# The record has to actually resolve -- the OCC#7465 defect class
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_core_resolves_the_written_record_to_the_executed_pass(
    tmp_path: Path,
) -> None:
    """Driven through core's REAL resolver, not a re-implementation.

    ``resolve_supersession`` key-validates a record's declared
    ``(ticket, item, check_type)`` against the key it is filed under. The
    emitter's supersede renderer hardcoded ``command`` for a ``test_passes``
    item, so its rebinds silently never applied (live: OCC#7465). If this
    writer repeated that, the receipt would be written, pushed, merged -- and
    eligibility would still report the PENDING base.
    """
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])
    _write_born_pending(occ_root, PASSING_CHECK)

    _execute(occ_root, tmp_path)

    resolution = resolve_supersession(
        occ_root / "drift" / "dod_receipts",
        TICKET,
        ITEM,
        "test_passes",
        current_pr_number=PR_NUMBER,
    )

    assert resolution is not None
    assert resolution.error is None
    assert resolution.tombstoned is False
    assert resolution.receipt is not None
    assert resolution.receipt.status is EnumReceiptStatus.PASS


@pytest.mark.unit
def test_the_written_record_validates_as_a_supersession_record(
    tmp_path: Path,
) -> None:
    """``ModelReceiptSupersession`` is ``extra="forbid"`` and frozen."""
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])
    _write_born_pending(occ_root, PASSING_CHECK)

    _execute(occ_root, tmp_path)

    record = ModelReceiptSupersession.model_validate(
        _load(_supersede_files(occ_root)[0])
    )
    assert record.tombstone is False
    assert record.replacement is not None
    assert record.replacement.contract_entry_sha256 is not None
    assert record.supersedes.endswith(f"{ITEM}/test_passes.yaml")


# ---------------------------------------------------------------------------
# It has to be a real execution
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_real_passing_command_yields_a_pass_receipt_with_its_real_output(
    tmp_path: Path,
) -> None:
    """Real subprocess, real exit status, real captured stdout."""
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])

    outcome = _execute(occ_root, tmp_path)

    receipt = ModelDodReceipt.model_validate(
        _load(_receipt_dir(occ_root) / "test_passes.yaml")
    )
    assert receipt.status is EnumReceiptStatus.PASS
    assert receipt.exit_code == 0
    assert "1 passed" in receipt.probe_stdout
    assert outcome.executed == 1


@pytest.mark.unit
def test_a_real_failing_command_yields_fail_and_never_pass(tmp_path: Path) -> None:
    """The adversarial case. A red check must produce a red receipt.

    If this ever produced PASS the runner would be a more efficient version of
    exactly the dishonesty this ticket was filed about.
    """
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", FAILING_CHECK)])

    _execute(occ_root, tmp_path)

    receipt = ModelDodReceipt.model_validate(
        _load(_receipt_dir(occ_root) / "test_passes.yaml")
    )
    assert receipt.status is EnumReceiptStatus.FAIL
    assert receipt.exit_code == 1
    assert "assert False" in receipt.probe_stdout


@pytest.mark.unit
def test_the_command_executed_is_the_contracts_declared_check_value(
    tmp_path: Path,
) -> None:
    """Verbatim -- which is also what satisfies OMN-15459 S2 by construction.

    S2 requires a replacement to reference the artifact family of the item it
    replaces. Re-deriving the command here (rather than reading it from the
    contract) would reopen the OCC#5534 laundering channel where one probe
    became the authoritative proof of N distinct bars.
    """
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])

    _execute(occ_root, tmp_path)

    receipt = ModelDodReceipt.model_validate(
        _load(_receipt_dir(occ_root) / "test_passes.yaml")
    )
    assert receipt.check_value == PASSING_CHECK
    assert receipt.probe_command == PASSING_CHECK


# ---------------------------------------------------------------------------
# Scope, idempotency, and the gates the receipt has to survive
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_item_already_passing_at_this_head_is_left_alone(tmp_path: Path) -> None:
    """Idempotent. Re-running must not append a record for settled evidence."""
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])
    _execute(occ_root, tmp_path)

    outcome = _execute(occ_root, tmp_path)

    assert outcome.executed == 0
    assert outcome.skipped_already_pass == 1
    assert _supersede_files(occ_root) == []


@pytest.mark.unit
def test_check_types_the_runner_cannot_honestly_execute_are_untouched(
    tmp_path: Path,
) -> None:
    """A ``command`` item is not this runner's business.

    The runner covers exactly what it can execute honestly in a product
    checkout. Silently taking over other check types would make it a second,
    competing producer.
    """
    occ_root = _occ_root(
        tmp_path,
        [
            _item("dod-occ-evidence-binding", "command", "gh pr view 2210"),
            _item(ITEM, "test_passes", PASSING_CHECK),
        ],
    )

    outcome = _execute(occ_root, tmp_path)

    assert not (_receipt_dir(occ_root, "dod-occ-evidence-binding")).exists()
    assert outcome.executed == 1


@pytest.mark.unit
def test_no_machine_specific_absolute_path_reaches_the_probe_fields(
    tmp_path: Path,
) -> None:
    """OMN-15710 ABS_PATH prefixes must not reach the receipt.

    The runner executes inside a CI workspace whose absolute path is
    machine-specific and unreproducible. Leaking it into ``check_value`` or
    ``probe_command`` would hard-fail OCC's Receipt Honesty Gate on the very
    companion this runner is trying to unblock.
    """
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])

    _execute(occ_root, tmp_path)

    receipt = ModelDodReceipt.model_validate(
        _load(_receipt_dir(occ_root) / "test_passes.yaml")
    )
    for field in (receipt.check_value, receipt.probe_command):
        for prefix in _ABS_PATH_PREFIXES:
            assert prefix not in field, (
                f"{prefix!r} leaked into a receipt field: {field!r}"
            )
    assert receipt.working_dir is None


@pytest.mark.unit
def test_the_receipt_is_not_self_attested_into_advisory(tmp_path: Path) -> None:
    """``verifier == runner`` silently downgrades PASS to ADVISORY.

    ADVISORY is non-PASS, so a self-attested receipt would leave the companion
    blocked while *looking* like the runner had done its job -- the most
    expensive possible failure mode, because it is invisible.
    """
    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])

    _execute(occ_root, tmp_path)

    receipt = ModelDodReceipt.model_validate(
        _load(_receipt_dir(occ_root) / "test_passes.yaml")
    )
    assert receipt.runner != receipt.verifier
    assert receipt.status is EnumReceiptStatus.PASS


@pytest.mark.unit
def test_the_contract_entry_hash_is_recomputed_from_this_companion(
    tmp_path: Path,
) -> None:
    """Copied hashes are how a receipt ends up bound to a contract it never saw."""
    from omnibase_core.validation.validator_receipt_gate import (
        compute_contract_entry_sha256,
    )

    occ_root = _occ_root(tmp_path, [_item(ITEM, "test_passes", PASSING_CHECK)])

    _execute(occ_root, tmp_path)

    receipt = ModelDodReceipt.model_validate(
        _load(_receipt_dir(occ_root) / "test_passes.yaml")
    )
    contract = yaml.safe_load(
        (occ_root / "contracts" / f"{TICKET}.yaml").read_text(encoding="utf-8")
    )
    assert receipt.contract_entry_sha256 == compute_contract_entry_sha256(
        contract, ITEM
    )


@pytest.mark.unit
def test_a_ticket_with_no_contract_in_the_companion_writes_nothing(
    tmp_path: Path,
) -> None:
    """Fail-soft on absence, never fabricate.

    A cited ticket whose contract is not in this companion is not this
    runner's to invent. Preflight already reports MISSING_CONTRACT for it.
    """
    occ_root = tmp_path / "occ"
    (occ_root / "contracts").mkdir(parents=True)

    outcome = _execute(occ_root, tmp_path)

    assert outcome.executed == 0
    assert outcome.wrote == ()
    assert TICKET in outcome.tickets_without_contract


# ---------------------------------------------------------------------------
# The Evidence-Source race
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Evidence-Source: OCC#7498", 7498),
        ("intro\nEvidence-Source: OCC#7498\ntrailer", 7498),
        ("evidence-source:   OCC#12", 12),
        ("Evidence-Source: OCC#7498\nEvidence-Source: OCC#9999", 7498),
        ("no stamp at all", None),
        ("Evidence-Source: deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", None),
        ("", None),
    ],
)
def test_evidence_source_parsing_matches_the_preflight_grammar(
    body: str, expected: int | None
) -> None:
    """Same grammar the preflight reusable uses; first match wins.

    A parser that disagreed with preflight would send the runner to a
    different companion than the one being evaluated -- it would write a
    perfectly good receipt into a branch nothing reads. The bare-SHA form
    returns None on purpose: there is no branch to push to, so the runner
    reports that rather than guessing.
    """
    assert runner.parse_evidence_source(body) == expected
