# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-19050: "already passes" means already passes for this code and this check.

The runner skips a key whose active receipt is PASS. It used to check only the
status, so a PASS recorded at any earlier head, against any earlier check
definition, counted forever:

* A fresh head could not get a fresh receipt. On the omnimarket#2839
  companion, the only way to make the runner execute at the new head was to
  delete the record it had already written (OCC 22261d23a8).
* A corrected check definition was not re-executed if the old definition had
  happened to pass.

What this module pins:

* A PASS covers the current run only when it was observed on the same code
  and bound to the current contract entry. The code is the tree when both
  sides carry one, and otherwise the commit. Anything else re-executes.
* A re-execution states why in the record it files: the prior attempt's
  status and head, and the check definition change when there was one. The
  record the #2839 runner filed at a second attempt said "The base receipt
  records status FAIL: the check was declared but not executed", which was
  false for an attempt that had executed.
* ``tree_sha`` is written only when the caller supplies it. ``ModelDodReceipt``
  is ``extra="forbid"``, so a record that carries the field is rejected by
  every reader still on an ``omnibase_core`` without it. That includes the
  receipt-gate core ref every repository pins. The workflow therefore does not
  pass it yet, and a test here keeps it that way until the readers have moved.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import occ_receipt_runner as runner  # noqa: E402

TICKET = "OMN-19378"
ITEM = "dod-occ-diff-derived-behavior-proof-pr-2839"
PR_NUMBER = 2839
REPO = "OmniNode-ai/omnimarket"
BRANCH = "jonah/omn-19378-retire-stale-rebuild-classifier"

FIRST_HEAD = "f490fba5" + "0" * 32
EMPTY_COMMIT_HEAD = "9c09739b" + "0" * 32
FIXED_HEAD = "668a565b" + "1" * 32
SHARED_TREE = "7dceb0ed" + "0" * 32
FIXED_TREE = "5a1f0c3e" + "2" * 32

PASSING_CHECK = "printf '14 passed in 6.42s\\n'"
AS_GENERATED_CHECK = "printf 'ERROR: file or directory not found\\n'; exit 4"


def _contract(check_value: str) -> dict[str, Any]:
    return {
        "ticket_id": TICKET,
        "title": "retire the dormant local rebuild classifier",
        "dod_evidence": [
            {
                "id": ITEM,
                "description": "the diff-derived behaviour proof",
                "source": "generated",
                "checks": [{"check_type": "test_passes", "check_value": check_value}],
            }
        ],
    }


def _occ_root(tmp_path: Path, check_value: str) -> Path:
    root = tmp_path / "occ"
    (root / "contracts").mkdir(parents=True)
    _set_check(root, check_value)
    base = _receipt_dir(root) / "test_passes.yaml"
    base.parent.mkdir(parents=True)
    base.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "ticket_id": TICKET,
                "evidence_item_id": ITEM,
                "check_type": "test_passes",
                "check_value": check_value,
                "status": "PENDING",
                "run_timestamp": datetime(2026, 9, 24, 7, 37, tzinfo=UTC),
                "commit_sha": FIRST_HEAD,
                "runner": "node_pr_lifecycle_fix_effect",
                "verifier": "occ-evidence-source-autobind",
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
    return root


def _set_check(occ_root: Path, check_value: str) -> None:
    (occ_root / "contracts" / f"{TICKET}.yaml").write_text(
        yaml.safe_dump(_contract(check_value), sort_keys=True), encoding="utf-8"
    )


def _receipt_dir(occ_root: Path) -> Path:
    return occ_root / "drift" / "dod_receipts" / TICKET / ITEM


def _execute(
    occ_root: Path,
    product_root: Path,
    *,
    head_sha: str,
    tree_sha: str | None = None,
) -> runner.RunnerOutcome:
    # tree_sha is passed only when a test supplies one, so every head-aware
    # test also runs against a runner that predates the parameter.
    extra: dict[str, str] = {} if tree_sha is None else {"tree_sha": tree_sha}
    return runner.run(
        occ_root=occ_root,
        product_root=product_root,
        ticket_ids=(TICKET,),
        pr_number=PR_NUMBER,
        repo=REPO,
        head_sha=head_sha,
        branch=BRANCH,
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
        **extra,
    )


def _records(occ_root: Path) -> list[dict[str, Any]]:
    """Every supersede record, oldest attempt first."""

    def attempt(path: Path) -> int:
        token = path.name[: -len(".yaml")].rsplit(".", 1)[-1]
        return int(token) if token.isdigit() and token != str(PR_NUMBER) else 1

    paths = sorted(
        _receipt_dir(occ_root).glob("test_passes.supersede.*.yaml"), key=attempt
    )
    return [yaml.safe_load(path.read_text(encoding="utf-8")) for path in paths]


# --------------------------------------------------------------------------- #
# The shortcut is head-aware
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_pass_at_this_head_is_not_re_executed(tmp_path: Path) -> None:
    """Idempotent at one head: a body edit re-triggers and changes nothing."""
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    again = _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    assert again.executed == 0
    assert again.skipped_already_pass == 1
    assert len(_records(occ_root)) == 1


@pytest.mark.unit
def test_a_fresh_head_gets_a_fresh_receipt(tmp_path: Path) -> None:
    """No record has to be deleted for the runner to observe a new head."""
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    again = _execute(occ_root, tmp_path, head_sha=FIXED_HEAD)

    assert again.executed == 1
    records = _records(occ_root)
    assert [r["replacement"]["commit_sha"] for r in records] == [
        FIRST_HEAD,
        FIXED_HEAD,
    ]


@pytest.mark.unit
def test_a_changed_check_definition_is_re_executed_at_the_same_head(
    tmp_path: Path,
) -> None:
    """A PASS against the old definition does not cover the new one."""
    occ_root = _occ_root(tmp_path, "printf 'old check\\n'")
    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)
    _set_check(occ_root, PASSING_CHECK)

    again = _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    assert again.executed == 1
    assert len(_records(occ_root)) == 2


# --------------------------------------------------------------------------- #
# A re-execution says why
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_check_definition_fix_records_why(tmp_path: Path) -> None:
    """The #2839 correction, as the runner should have recorded it."""
    occ_root = _occ_root(tmp_path, AS_GENERATED_CHECK)
    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)
    old_entry = compute_contract_entry_sha256(_contract(AS_GENERATED_CHECK), ITEM)
    _set_check(occ_root, PASSING_CHECK)
    new_entry = compute_contract_entry_sha256(_contract(PASSING_CHECK), ITEM)

    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    reason = _records(occ_root)[-1]["reason"]
    assert "check definition changed" in reason
    assert old_entry in reason
    assert new_entry in reason
    assert "FAIL" in reason
    assert "declared but not executed" not in reason


@pytest.mark.unit
def test_a_re_execution_at_a_new_head_names_the_prior_attempt(tmp_path: Path) -> None:
    occ_root = _occ_root(tmp_path, AS_GENERATED_CHECK)
    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    _execute(occ_root, tmp_path, head_sha=FIXED_HEAD)

    reason = _records(occ_root)[-1]["reason"]
    assert FIRST_HEAD in reason
    assert FIXED_HEAD in reason
    assert "check definition changed" not in reason


@pytest.mark.unit
def test_the_first_execution_keeps_its_original_reason(tmp_path: Path) -> None:
    occ_root = _occ_root(tmp_path, PASSING_CHECK)

    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    reason = _records(occ_root)[0]["reason"]
    assert "records status PENDING" in reason


# --------------------------------------------------------------------------- #
# tree_sha: tree-aware when supplied, and never written unless supplied
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_tree_sha_is_not_written_unless_supplied(tmp_path: Path) -> None:
    """Every reader on an older core would reject a record that carried it."""
    occ_root = _occ_root(tmp_path, PASSING_CHECK)

    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    assert "tree_sha" not in _records(occ_root)[0]["replacement"]


@pytest.mark.unit
def test_a_supplied_tree_sha_is_recorded(tmp_path: Path) -> None:
    occ_root = _occ_root(tmp_path, PASSING_CHECK)

    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD, tree_sha=SHARED_TREE)

    assert _records(occ_root)[0]["replacement"]["tree_sha"] == SHARED_TREE


@pytest.mark.unit
def test_an_empty_commit_over_a_passing_tree_is_not_re_executed(
    tmp_path: Path,
) -> None:
    """Same tree, same check: the PASS already covers it, so nothing is filed."""
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD, tree_sha=SHARED_TREE)

    again = _execute(
        occ_root, tmp_path, head_sha=EMPTY_COMMIT_HEAD, tree_sha=SHARED_TREE
    )

    assert again.executed == 0
    assert again.skipped_already_pass == 1


@pytest.mark.unit
def test_a_new_tree_is_re_executed(tmp_path: Path) -> None:
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _execute(occ_root, tmp_path, head_sha=FIRST_HEAD, tree_sha=SHARED_TREE)

    again = _execute(occ_root, tmp_path, head_sha=FIXED_HEAD, tree_sha=FIXED_TREE)

    assert again.executed == 1


@pytest.mark.unit
def test_the_workflow_does_not_pass_tree_sha_yet() -> None:
    """Rollout guard. Delete this test in the change that enables the flag.

    Enable it only once every reader that parses runner records runs an
    omnibase_core whose ModelDodReceipt declares tree_sha. That means OCC's
    lock and gates, this repository's lock and receipt-gate pin, and the
    receipt-gate core ref in every repository whose tickets share a contract
    with an omnimarket PR.
    """
    workflow = (
        _REPO_ROOT / ".github" / "workflows" / "occ-receipt-runner.yml"
    ).read_text(encoding="utf-8")

    assert "--tree-sha" not in workflow


# --------------------------------------------------------------------------- #
# A check declared for another repository is never executed here
# --------------------------------------------------------------------------- #
#
# The runner executes in this repository's checkout. A check whose declared
# cwd names another repository cannot be observed here: its test target is not
# in this tree, pytest exits 4, and the runner files a FAIL that says nothing
# about the code it names. Measured on OCC dev: omnimarket#2767 filed FAIL
# records against an omnibase_core check of OMN-19050 ("file or directory not
# found"), and the head-aware shortcut above made it worse: at omnimarket#2846
# the runner re-executed an omnibase_core PASS recorded at an omnibase_core
# head and superseded it with a FAIL.


def _set_check_with_cwd(occ_root: Path, check_value: str, cwd: str) -> None:
    contract = _contract(check_value)
    contract["dod_evidence"][0]["checks"][0]["cwd"] = cwd
    (occ_root / "contracts" / f"{TICKET}.yaml").write_text(
        yaml.safe_dump(contract, sort_keys=True), encoding="utf-8"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "cwd",
    [
        "${OMNI_HOME}/omnibase_core",
        "$OMNI_HOME/omnibase_core",
        "${OMNI_HOME}/omni_worktrees/OMN-19050/omnibase_core",
        "${OMNI_HOME}/omnibase_infra/scripts/deploy-agent",
        "${OMNI_HOME}",
        "/opt/elsewhere",
    ],
)
def test_a_check_declared_for_another_repository_is_not_executed(
    tmp_path: Path, cwd: str
) -> None:
    occ_root = _occ_root(tmp_path, AS_GENERATED_CHECK)
    _set_check_with_cwd(occ_root, AS_GENERATED_CHECK, cwd)

    outcome = _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    assert outcome.executed == 0
    assert outcome.skipped_other_repo == 1
    assert outcome.failures == ()
    assert _records(occ_root) == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "cwd",
    [
        "${OMNI_HOME}/omnimarket",
        "$OMNI_HOME/omnimarket/",
        "${OMNI_HOME}/omni_worktrees/OMN-17450/omnimarket",
        "${OMNI_HOME}/omnimarket/tests",
    ],
)
def test_a_check_declared_for_this_repository_is_executed(
    tmp_path: Path, cwd: str
) -> None:
    occ_root = _occ_root(tmp_path, PASSING_CHECK)
    _set_check_with_cwd(occ_root, PASSING_CHECK, cwd)

    outcome = _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    assert outcome.executed == 1
    assert outcome.skipped_other_repo == 0


@pytest.mark.unit
def test_a_check_with_no_declared_cwd_is_still_executed(tmp_path: Path) -> None:
    occ_root = _occ_root(tmp_path, PASSING_CHECK)

    outcome = _execute(occ_root, tmp_path, head_sha=FIRST_HEAD)

    assert outcome.executed == 1
    assert outcome.skipped_other_repo == 0


@pytest.mark.unit
def test_another_repositorys_pass_is_never_superseded_from_here(
    tmp_path: Path,
) -> None:
    """The omnimarket#2846 companion case: a core PASS stays the active record."""
    occ_root = _occ_root(tmp_path, AS_GENERATED_CHECK)
    _set_check_with_cwd(occ_root, AS_GENERATED_CHECK, "${OMNI_HOME}/omnibase_core")
    base = _receipt_dir(occ_root) / "test_passes.yaml"
    body = yaml.safe_load(base.read_text(encoding="utf-8"))
    body["status"] = "PASS"
    body["commit_sha"] = "5e96acb7" + "0" * 32
    base.write_text(yaml.safe_dump(body, sort_keys=True), encoding="utf-8")

    outcome = _execute(occ_root, tmp_path, head_sha=FIXED_HEAD)

    assert outcome.executed == 0
    assert _records(occ_root) == []
    assert yaml.safe_load(base.read_text(encoding="utf-8"))["status"] == "PASS"
