# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19050 — one pull request may record more than one executed attempt.

Measured on omnimarket#2751 (2026-09-21). The runner executed the declared
behaviour-proof check at head ``bab99887``; it failed for an environment
reason, a depth-1 checkout with no tags, and the runner filed
``test_passes.supersede.2751.yaml`` with ``status: FAIL``. The cause was fixed
at ``668a565b1``, the check re-executed green, and the runner REFUSED the
second record because the filename was keyed on the PR number alone and was
already taken. Preflight kept reading a non-pass receipt, the companion could
not merge, and the product PR could not merge behind it.

So one pull request got exactly one executed attempt, ever. A check that
failed for any reason blocked that PR through the evidence chain permanently,
and the only escapes were abandoning the PR number or hand-authoring a
receipt -- which the eligibility validator forbids in its own detail text.

Two further defects rode along in the same run, and both are covered here:

* the refusal was appended to the runner's ``failures`` list and the job
  still concluded success;
* the closing notice read that every runner-covered check already resolved
  PASS while the covered check resolved FAIL.

What this module pins:

* the FIRST attempt keeps the historical filename, so nothing already on a
  companion branch changes shape;
* a SECOND attempt is filed as ``<check>.supersede.<pr>.<NNNN>.yaml`` and
  carries the new result;
* nothing is ever overwritten, which is the append-only rule this evidence
  chain rests on;
* a re-executed check that still fails leaves the key failing;
* a refusal to RECORD is not a failed check and cannot exit zero, while a
  failed check still exits zero because a receipt carries that fact.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import occ_receipt_runner as runner  # noqa: E402

TICKET = "OMN-18868"
ITEM = "dod-occ-diff-derived-behavior-proof-pr-2751"
PR_NUMBER = 2751
HEAD_SHA = "a" * 40
SECOND_SHA = "b" * 40
BRANCH = "jonah/omn-18868-wire-compatibility-gate"
REPO = "OmniNode-ai/omnimarket"

PASSING_CHECK = "printf '31 passed in 13.14s\\n'"
FAILING_CHECK = "printf 'ERROR no tags in a depth-1 checkout\\n'; exit 1"


def _occ_root(tmp_path: Path, check_value: str) -> Path:
    root = tmp_path / "occ"
    (root / "contracts").mkdir(parents=True, exist_ok=True)
    (root / "contracts" / f"{TICKET}.yaml").write_text(
        yaml.safe_dump(
            {
                "ticket_id": TICKET,
                "title": "wire-compatibility gate",
                "dod_evidence": [
                    {
                        "id": ITEM,
                        "description": "the diff-derived behaviour proof",
                        "source": "generated",
                        "checks": [
                            {"check_type": "test_passes", "check_value": check_value}
                        ],
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return root


def _receipt_dir(occ_root: Path) -> Path:
    return occ_root / "drift" / "dod_receipts" / TICKET / ITEM


def _write_born_pending(occ_root: Path, check_value: str) -> Path:
    """The honest born receipt the minting producer writes: PENDING, no stdout."""
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
                "run_timestamp": datetime(2026, 9, 21, 10, 0, tzinfo=UTC),
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


def _execute(
    occ_root: Path, product_root: Path, *, head_sha: str = HEAD_SHA
) -> runner.RunnerOutcome:
    return runner.run(
        occ_root=occ_root,
        product_root=product_root,
        ticket_ids=(TICKET,),
        pr_number=PR_NUMBER,
        repo=REPO,
        head_sha=head_sha,
        branch=BRANCH,
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
    )


def _records(occ_root: Path) -> list[Path]:
    directory = _receipt_dir(occ_root)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("test_passes.supersede.*.yaml"))


def _status_of(path: Path) -> str:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    replacement: dict[str, Any] = data["replacement"]
    return str(replacement["status"])


def _rewrite_check(occ_root: Path, check_value: str) -> None:
    """Simulate the fix landing: the same declared item, now passing."""
    path = occ_root / "contracts" / f"{TICKET}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["dod_evidence"][0]["checks"][0]["check_value"] = check_value
    path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- #
# The latch
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_first_attempt_keeps_the_historical_filename(tmp_path: Path) -> None:
    """Nothing already on a companion branch changes shape."""
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _write_born_pending(occ_root, PASSING_CHECK)

    outcome = _execute(occ_root, tmp_path)

    assert outcome.executed == 1
    assert not outcome.write_refusals
    assert [p.name for p in _records(occ_root)] == [
        f"test_passes.supersede.{PR_NUMBER}.yaml"
    ]


@pytest.mark.unit
def test_a_fixed_check_records_a_second_attempt(tmp_path: Path) -> None:
    """The #2751 sequence: FAIL, fix, re-execute. The second record is filed."""
    occ_root = _occ_root(tmp_path, FAILING_CHECK)
    _write_born_pending(occ_root, FAILING_CHECK)

    first = _execute(occ_root, tmp_path)
    assert first.executed == 1
    assert first.failures  # the check really did fail
    assert not first.write_refusals

    _rewrite_check(occ_root, PASSING_CHECK)
    second = _execute(occ_root, tmp_path, head_sha=SECOND_SHA)

    assert second.executed == 1
    assert not second.failures
    assert not second.write_refusals

    names = [p.name for p in _records(occ_root)]
    assert names == [
        f"test_passes.supersede.{PR_NUMBER}.0002.yaml",
        f"test_passes.supersede.{PR_NUMBER}.yaml",
    ]
    assert _status_of(_receipt_dir(occ_root) / names[1]) == "FAIL"
    assert _status_of(_receipt_dir(occ_root) / names[0]) == "PASS"


@pytest.mark.unit
def test_the_first_record_is_never_overwritten(tmp_path: Path) -> None:
    """Append-only in the literal sense: the FAIL record's bytes are untouched."""
    occ_root = _occ_root(tmp_path, FAILING_CHECK)
    _write_born_pending(occ_root, FAILING_CHECK)
    _execute(occ_root, tmp_path)

    first_record = _receipt_dir(occ_root) / f"test_passes.supersede.{PR_NUMBER}.yaml"
    before = first_record.read_bytes()

    _rewrite_check(occ_root, PASSING_CHECK)
    _execute(occ_root, tmp_path, head_sha=SECOND_SHA)

    assert first_record.read_bytes() == before


@pytest.mark.unit
def test_a_still_failing_recheck_stays_failing(tmp_path: Path) -> None:
    """A second attempt is not a second chance at a verdict."""
    occ_root = _occ_root(tmp_path, FAILING_CHECK)
    _write_born_pending(occ_root, FAILING_CHECK)

    _execute(occ_root, tmp_path)
    second = _execute(occ_root, tmp_path, head_sha=SECOND_SHA)

    assert second.failures
    names = [p.name for p in _records(occ_root)]
    assert f"test_passes.supersede.{PR_NUMBER}.0002.yaml" in names
    for name in names:
        assert _status_of(_receipt_dir(occ_root) / name) == "FAIL"


@pytest.mark.unit
def test_a_passing_key_is_not_re_executed(tmp_path: Path) -> None:
    """The runner reads its own attempt shape back without a newer core."""
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _write_born_pending(occ_root, PASSING_CHECK)
    _execute(occ_root, tmp_path)

    # Force the attempt-scoped shape, which an older installed omnibase_core
    # cannot see at all -- this asserts the runner does not depend on it.
    first = _receipt_dir(occ_root) / f"test_passes.supersede.{PR_NUMBER}.yaml"
    first.rename(first.with_name(f"test_passes.supersede.{PR_NUMBER}.0002.yaml"))

    again = _execute(occ_root, tmp_path)

    assert again.executed == 0
    assert again.skipped_already_pass == 1


# --------------------------------------------------------------------------- #
# The honesty defects that rode along
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_failed_check_still_exits_zero(tmp_path: Path, capsys: Any) -> None:
    """Unchanged and deliberate: the FAIL receipt carries that fact."""
    occ_root = _occ_root(tmp_path, FAILING_CHECK)
    _write_born_pending(occ_root, FAILING_CHECK)

    exit_code = runner.main(
        [
            "--occ-root",
            str(occ_root),
            "--product-root",
            str(tmp_path),
            "--pr-number",
            str(PR_NUMBER),
            "--repo",
            REPO,
            "--head-sha",
            HEAD_SHA,
            "--branch",
            BRANCH,
            "--run-url",
            "https://example.invalid/run/1",
            "--tickets",
            TICKET,
        ]
    )

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["failures"]
    assert summary["write_refusals"] == []
    assert summary["recorded_everything_executed"] is True


@pytest.mark.unit
def test_a_refusal_to_record_cannot_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A refusal leaves NOTHING downstream carrying the fact.

    The job's exit status is the only surface that can report it, which is
    exactly what did not happen on omnimarket#2751: the refusal was appended
    to ``failures`` and the job concluded success.
    """
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _write_born_pending(occ_root, PASSING_CHECK)
    monkeypatch.setattr(runner, "_next_record_path", lambda *_a, **_k: None)

    exit_code = runner.main(
        [
            "--occ-root",
            str(occ_root),
            "--product-root",
            str(tmp_path),
            "--pr-number",
            str(PR_NUMBER),
            "--repo",
            REPO,
            "--head-sha",
            HEAD_SHA,
            "--branch",
            BRANCH,
            "--run-url",
            "https://example.invalid/run/1",
            "--tickets",
            TICKET,
        ]
    )

    assert exit_code == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["write_refusals"]
    assert summary["recorded_everything_executed"] is False


@pytest.mark.unit
def test_attempt_slots_are_bounded(tmp_path: Path) -> None:
    """The bound exists so a wedged loop cannot append without end."""
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    base = _write_born_pending(occ_root, PASSING_CHECK)
    directory = _receipt_dir(occ_root)
    (directory / f"test_passes.supersede.{PR_NUMBER}.yaml").write_text(
        "placeholder: true\n", encoding="utf-8"
    )
    for attempt in range(2, runner.MAX_ATTEMPTS_PER_CONSUMER + 1):
        (
            directory / f"test_passes.supersede.{PR_NUMBER}.{attempt:04d}.yaml"
        ).write_text("placeholder: true\n", encoding="utf-8")

    assert runner._next_record_path(base, "test_passes", PR_NUMBER) is None


@pytest.mark.unit
def test_status_enum_round_trips_from_an_attempt_record(tmp_path: Path) -> None:
    """The attempt reader returns a typed status, not a string."""
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _write_born_pending(occ_root, PASSING_CHECK)
    _execute(occ_root, tmp_path)
    first = _receipt_dir(occ_root) / f"test_passes.supersede.{PR_NUMBER}.yaml"
    first.rename(first.with_name(f"test_passes.supersede.{PR_NUMBER}.0002.yaml"))

    status = runner._latest_attempt_status(
        occ_root / "drift" / "dod_receipts", TICKET, ITEM, "test_passes", PR_NUMBER
    )

    assert status is EnumReceiptStatus.PASS
