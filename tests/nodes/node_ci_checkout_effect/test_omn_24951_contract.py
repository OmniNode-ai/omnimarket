# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket contract of the ci-checkout serve-only change binds its criteria to a reading that can fail.

The contract's ledger checks run as the local done gate runs them (bash, the ledger path in the variable the
check names, the repository as the working directory) against scratch ledgers. A row passes only when it was read after the h201
dev runtime was deployed at a revision whose node contract declares no subscribe_topics, and the row written
before the change (ledger row 2214, whose observed cell records the h201 failure) is carried here and must fail
every check.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-24951.yaml"
NODE_CONTRACT = Path("src/omnimarket/nodes/node_ci_checkout_effect/contract.yaml")
PYTEST_CHECK = re.compile(r"\bpytest\b")
# The ledger path variable, read from each check (the check names it; this file does not spell it).
LEDGER_VARIABLE = re.compile(r"\$\{?([A-Z_]*LEDGER[A-Z_]*)\}?")
LEDGER_CHECKS = {
    "AC1": "dod-omn24951-ac1-ledger",
    "AC1-h201": "dod-omn24951-ac1-h201",
    "AC2": "dod-omn24951-ac2-ledger",
    "AC3": "dod-omn24951-ac3-receipt",
}

# Ledger row 2214 (2026-11-17T14:03:00Z) as written before this change, principal cell redacted. Its observed
# cell records the bug: the request failed on the h201 dev runtime with NodeOverlayError and a failed terminal.
ROW_2214 = (
    "2026-11-17T14:03:00Z | STATUS | lane=run6062-1243 | principal=operator | ticket=OMN-24951"
    " | actor=claude:sonnet:remote-lane | model=sonnet | parent=orchestrator-31d9-swarm"
    " | item=throughput | band=B3 | pr=omnimarket#6062"
    " | installed=h051:6efbe3ee5027f6efb67d0b81a1421f0dda32b4e5"
    " | observed=own send 13:20:26Z of onex ci checkout --host h051 --lane dev --bus-lane dev for OmniNode-ai/omnimarket@6efbe3ee5027 exit 0 status=served source=bare-mirror upstream_fetches=0, serve.log line 2026-11-17 13:20:36,138 status=served by onex-ci-checkout-serve.service MainPID 3619039 active since 2026-11-16T18:14:14Z; the same request on the h201 dev runtime log at 13:20:36 read: Dispatcher dispatcher.auto.node_ci_checkout_effect.HandlerCiCheckout failed: NodeOverlayError: no host to answer for: pass host_name or set ONEX_CI_CHECKOUT_HOST_NAME, then a failed terminal on onex.evt.omnimarket.ci-checkout-failed.v1 at 13:20:39"
    " | read_at=2026-11-17T14:00:11Z | host=h051"
    " | h201=NODE-AUTO-WIRED-IN-RUNTIME-AND-FAILING-ON-EVERY-REQUEST"
    " | cause=two parts: (1) the node contract declares event_bus.subscribe_topics so the h201 dev runtime builds HandlerCiCheckout with no host name and no overlay and every request fails there while h051 serve answers; (2) overlays merged_change_runners has no row for omnimarket#6062 so the pass reads NOT_RUNNING no_declared_runner (reader run 13:19:00Z in_build true)"
    " | owner=orchestrator-31d9-swarm lands the omnimarket draft PR (contract subscribe_topics empty, head 9c8f6b3c3757) and the overlays draft PR (runner row host_unit h051, head d309550e45c1), both opened by this lane with a Lab: line; the dev runtime deploy installs the contract after merge"
    " | installer_receipt=prior lane run6062-1722 STATUS 2026-11-16T18:15:22Z (ci-checkout unit uninstall then install through the node's own installer); this lane ran no installer and edited no host file or unit or plist by hand"
    " | via=relay:h051 | req=97e94b6d-de4b-42fc-a036-79918061f305 | via=bus:h200"
    " | the h201 runtime answer is not read, the node is not meant to serve there (overlay hosts holds h051 only)"
)


def _load() -> dict[str, Any]:
    loaded = yaml.safe_load(CONTRACT.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _acceptance(contract: dict[str, Any]) -> dict[str, str]:
    return {
        a["id"]: a["statement"]
        for req in contract["requirements"]
        for a in req["acceptance"]
    }


def _evidence_for(contract: dict[str, Any], ac: str) -> list[dict[str, Any]]:
    # The ledger checks are local_done_gate and unbound: the hosted dod-verify refuses a bound check it cannot run.
    ledger_ids = {v for k, v in LEDGER_CHECKS.items() if k.split("-")[0] == ac}
    return [
        e
        for e in contract["dod_evidence"]
        if ac in e.get("binds_ac", []) or e["id"] in ledger_ids
    ]


def _ledger_checks(contract: dict[str, Any], ac: str) -> list[str]:
    return [
        c["check_value"]
        for e in _evidence_for(contract, ac)
        if e.get("execution_scope") == "local_done_gate"
        for c in e["checks"]
        if c["check_type"] == "command" and LEDGER_VARIABLE.search(c["check_value"])
    ]


def _check(evidence_id: str) -> str:
    """The one ledger check of the named dod_evidence entry."""
    [entry] = [e for e in _load()["dod_evidence"] if e["id"] == evidence_id]
    assert entry.get("execution_scope") == "local_done_gate", entry
    [check] = entry["checks"]
    assert check["check_type"] == "command"
    assert isinstance(check["check_value"], str)
    return check["check_value"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A scratch repository: one revision whose node contract still subscribes, one that declares none."""
    root = tmp_path / "repo"
    (root / NODE_CONTRACT.parent).mkdir(parents=True)
    _git(root, "init", "-q")
    (root / NODE_CONTRACT).write_text(
        "event_bus:\n  subscribe_topics:\n    - onex.cmd.omnimarket.ci-checkout-requested.v1\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "before")
    before = _git(root, "rev-parse", "HEAD")
    (root / NODE_CONTRACT).write_text("event_bus:\n  subscribe_topics: []\n")
    _git(root, "commit", "-q", "-am", "after")
    return root, before, _git(root, "rev-parse", "HEAD")


def _good_row(deployed: str) -> str:
    return (
        "2026-11-18T10:00:00Z | STATUS | lane=probe | pr=omnimarket#6062 | "
        f"deployed=h201:{deployed} | deployed_at=2026-11-18T09:00:00Z | "
        "installed=h051:abc1234def | "
        "observed=ci-checkout OmniNode-ai/omnimarket@abc1234def status=served by onex-ci-checkout-serve.service | "
        "correlation_id=7f0c3a52-9a51-4c43-9d1e-2f4a1b6c8d90 | h201_consumers=0 | h201_failed_terminals=0 | "
        "read_at=2026-11-18T09:30:00Z | receipt=installed onex-ci-checkout-serve.service on h051 | "
        "receipt_rev=abc1234def | unit_rev=abc1234def | text"
    )


def _run(check: str, ledger: Path, cwd: Path) -> int:
    """Run one contract check the way the done gate does, against a scratch ledger, in a scratch repository."""
    named = LEDGER_VARIABLE.search(check)
    assert named is not None, check
    return subprocess.run(
        ["bash", "-o", "pipefail", "-c", check],
        env={**scrub_git_location_env(os.environ), named.group(1): str(ledger)},
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    ).returncode


def _passes(check: str, row: str, tmp_path: Path, cwd: Path) -> bool:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(row + "\n")
    return _run(check, ledger, cwd) == 0


def test_omn_24951_contract_ac1_names_the_h051_serve_not_the_h201_runtime() -> None:
    ac1 = _acceptance(_load())["AC1"]
    assert "installed=h051:" in ac1
    assert "on h051 itself" in ac1
    # The reading the change makes impossible: a request answered by the dev runtime on h201.
    assert "dev runtime on h201 itself" not in ac1
    assert "installed=<host>" not in ac1


@pytest.mark.parametrize("name", sorted(LEDGER_CHECKS))
def test_omn_24951_contract_row_2214_fails_every_ledger_check(
    name: str, tmp_path: Path, repo: tuple[Path, str, str]
) -> None:
    """The row written before the change, whose observed cell names the h201 failure, satisfies no check."""
    assert "pr=omnimarket#6062" in ROW_2214
    assert "installed=h051:" in ROW_2214
    assert not _passes(_check(LEDGER_CHECKS[name]), ROW_2214, tmp_path, repo[0])


@pytest.mark.parametrize("name", sorted(LEDGER_CHECKS))
def test_omn_24951_contract_ledger_check_passes_a_row_read_after_the_deploy(
    name: str, tmp_path: Path, repo: tuple[Path, str, str]
) -> None:
    """Positive control for every rejection below: the full row passes each check."""
    root, _before, after = repo
    assert _passes(_check(LEDGER_CHECKS[name]), _good_row(after), tmp_path, root)
    ledger = tmp_path / "other.md"
    ledger.write_text("2026-11-18T10:00:00Z | STATUS | lane=other | text\n")
    assert _run(_check(LEDGER_CHECKS[name]), ledger, root) != 0


@pytest.mark.parametrize("name", ["AC1", "AC1-h201", "AC2"])
@pytest.mark.parametrize(
    "case",
    [
        "a deploy revision whose node contract still subscribes",
        "a deploy revision the repository does not hold",
        "a read before the deploy",
        "no deploy cell",
        "a deploy on another host",
    ],
)
def test_omn_24951_contract_ledger_check_ties_the_row_to_the_deploy(
    name: str, case: str, tmp_path: Path, repo: tuple[Path, str, str]
) -> None:
    root, before, after = repo
    row = {
        "a deploy revision whose node contract still subscribes": _good_row(before),
        "a deploy revision the repository does not hold": _good_row("0" * 40),
        "a read before the deploy": _good_row(after).replace(
            "read_at=2026-11-18T09:30:00Z", "read_at=2026-11-18T08:30:00Z"
        ),
        "no deploy cell": _good_row(after).replace(f"deployed=h201:{after} | ", ""),
        "a deploy on another host": _good_row(after).replace(
            "deployed=h201:", "deployed=h051:"
        ),
    }[case]
    assert not _passes(_check(LEDGER_CHECKS[name]), row, tmp_path, root), case


@pytest.mark.parametrize("name", ["AC1", "AC2"])
def test_omn_24951_contract_ledger_check_rejects_an_observed_cell_naming_h201(
    name: str, tmp_path: Path, repo: tuple[Path, str, str]
) -> None:
    """AC1's falsifier: the observed cell names a request read on h201."""
    root, _before, after = repo
    row = _good_row(after).replace("status=served by", "status=served on h201 by")
    assert not _passes(_check(LEDGER_CHECKS[name]), row, tmp_path, root)


def test_omn_24951_contract_ac1_ledger_check_wants_the_h051_install(
    tmp_path: Path, repo: tuple[Path, str, str]
) -> None:
    root, _before, after = repo
    row = _good_row(after).replace("installed=h051:", "installed=h201:")
    assert not _passes(_check(LEDGER_CHECKS["AC1"]), row, tmp_path, root)


@pytest.mark.parametrize(
    "case",
    [
        "h201_consumers=1",
        "h201_failed_terminals=1",
        "no correlation id",
    ],
)
def test_omn_24951_contract_h201_done_gate_reads_no_consumer_and_no_failed_terminal(
    case: str, tmp_path: Path, repo: tuple[Path, str, str]
) -> None:
    """The reading this diff changes: after the deploy, h201 wires no consumer and publishes no failed terminal."""
    root, _before, after = repo
    good = _good_row(after)
    row = {
        "h201_consumers=1": good.replace("h201_consumers=0", "h201_consumers=1"),
        "h201_failed_terminals=1": good.replace(
            "h201_failed_terminals=0", "h201_failed_terminals=1"
        ),
        "no correlation id": good.replace(
            "correlation_id=7f0c3a52-9a51-4c43-9d1e-2f4a1b6c8d90 | ", ""
        ),
    }[case]
    assert not _passes(_check(LEDGER_CHECKS["AC1-h201"]), row, tmp_path, root)
    assert "dod-omn24951-ac1-h201" in [e["id"] for e in _evidence_for(_load(), "AC1")]


def test_omn_24951_contract_ac2_ledger_check_takes_a_cause_and_who_must_act(
    tmp_path: Path, repo: tuple[Path, str, str]
) -> None:
    root, _before, _after = repo
    check = _check(LEDGER_CHECKS["AC2"])
    row = (
        "2026-11-18T10:00:00Z | STATUS | lane=probe | pr=omnimarket#6062 | "
        "cause=the dev deploy agent has not rolled the merge | must_act=dev deploy agent | text"
    )
    assert _passes(check, row, tmp_path, root)
    # An executor cell is not who must act: row 2214 carries cause= and actor= and fails.
    assert not _passes(check, row.replace("must_act=", "actor="), tmp_path, root)


@pytest.mark.parametrize(
    "case",
    [
        "the receipt only inside installer_receipt",
        "a hand-typed receipt with no revisions",
        "a unit read at another revision",
        "a receipt at another revision",
        "a receipt for another host",
    ],
)
def test_omn_24951_contract_ac3_receipt_check_is_anchored_and_compares_revisions(
    case: str, tmp_path: Path, repo: tuple[Path, str, str]
) -> None:
    root, _before, after = repo
    good = _good_row(after)
    receipt = "receipt=installed onex-ci-checkout-serve.service on h051"
    row = {
        "the receipt only inside installer_receipt": good.replace(
            receipt, "installer_" + receipt
        ),
        "a hand-typed receipt with no revisions": good.replace(
            " | receipt_rev=abc1234def | unit_rev=abc1234def", ""
        ),
        "a unit read at another revision": good.replace(
            "unit_rev=abc1234def", "unit_rev=fff9999eee"
        ),
        "a receipt at another revision": good.replace(
            "receipt_rev=abc1234def", "receipt_rev=fff9999eee"
        ),
        "a receipt for another host": good.replace(
            receipt, receipt.replace("on h051", "on h201")
        ),
    }[case]
    assert not _passes(_check(LEDGER_CHECKS["AC3"]), row, tmp_path, root), case


@pytest.mark.parametrize("ac", ["AC1", "AC2", "AC3"])
def test_omn_24951_contract_every_criterion_has_a_ledger_or_receipt_check_beside_its_pytest_proxy(
    ac: str,
) -> None:
    contract = _load()
    evidence = _evidence_for(contract, ac)
    scoped = [e for e in evidence if e.get("execution_scope") == "local_done_gate"]
    hosted_checks = [
        c["check_value"]
        for e in evidence
        if e.get("execution_scope") != "local_done_gate"
        for c in e["checks"]
        if c["check_type"] == "test_passes"
    ]
    assert scoped, f"{ac} is bound only to hosted checks"
    assert _ledger_checks(contract, ac), f"{ac} has no ledger check"
    # The pytest proxy stays for hosted CI, but it is never the only binding, and it is never absent.
    assert hosted_checks, f"{ac} has no hosted proxy"
    assert all(PYTEST_CHECK.search(check) for check in hosted_checks)


def test_omn_24951_contract_ac1_proxy_runs_the_shared_runtime_tests_not_only_this_file() -> (
    None
):
    [proxy] = [
        c["check_value"]
        for e in _evidence_for(_load(), "AC1")
        if e.get("execution_scope") != "local_done_gate"
        for c in e["checks"]
    ]
    assert "test_golden_chain_node_ci_checkout_effect.py" in proxy
    assert "shared_runtime" in proxy
