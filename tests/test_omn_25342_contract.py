# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shell checks of contracts/OMN-25342.yaml can fail (review of the PR at 51bdb423ac65, findings 2, 3 and 7).

Each check string is read from the contract file and run with ``bash`` in a throwaway git repository and a fixture
ledger, so what is proved is the check the Done gate runs. The old checks are kept below as the controls: each one
passes the input the new check refuses.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-25342.yaml"

# The checks as they stood at 51bdb423ac65.
OLD_AC3 = "! git diff --name-only HEAD~1 HEAD | grep -E '(^|/)(\\.service|\\.plist|\\.timer)$|^(etc|usr|Library)/'"

UNREACHABLE_ROW = (
    "2026-11-17T13:55:19Z | STATUS | lane=run6132-1243 | pr=omnimarket#6132 | installed=h201:13a7e30415fa | "
    "observed=unreachable: the command route is not subscribed | host unreachable"
)


def _entries() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return {entry["id"]: entry for entry in document["dod_evidence"]}


def _check(entry_id: str) -> str:
    (check,) = _entries()[entry_id]["checks"]
    value: str = check["check_value"]
    return value


def _ledger_variable() -> str:
    """The environment variable the checks read the ledger file through, taken from the check text itself."""
    match = re.search(r"\$\{?(\w*LEDGER\w*)", _check("dod-omn25342-ac1-status-row"))
    assert match is not None
    return match.group(1)


def _old_ac1() -> str:
    """The AC1 check as it stood at 51bdb423ac65: only ``installed=`` and ``observed=`` were required."""
    return f"grep -F 'pr=omnimarket#6132' \"${_ledger_variable()}\" | grep -F 'installed=' | grep -qF 'observed='"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
        },
    )
    return done.stdout.strip()


def _run(command: str, cwd: Path, ledger: Path | None = None) -> int:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)}
    if ledger is not None:
        env[_ledger_variable()] = str(ledger)
    return subprocess.run(
        ["bash", "-c", command], cwd=cwd, env=env, capture_output=True, text=True
    ).returncode


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "work")
    (path / "README.md").write_text("base\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")
    _git(path, "update-ref", "refs/remotes/origin/dev", "HEAD")
    return path


def _commit(repo: Path, name: str) -> None:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", name)


def _ledger(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("".join(f"{row}\n" for row in rows))
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short=12", "HEAD")


@pytest.mark.parametrize("name", ["deploy/x.service", "a/b/c.plist", "x.timer"])
def test_omn_25342_contract_ac3_refuses_a_unit_file_in_any_directory_in_an_earlier_commit(
    repo: Path, name: str
) -> None:
    _commit(repo, name)
    _commit(
        repo, "src/harmless.py"
    )  # the unit file is now two commits back: HEAD~1 no longer covers it
    assert _run(OLD_AC3, repo) == 0  # the old check passes it
    assert _run(_check("dod-omn25342-ac3-no-hand-edit"), repo) != 0


def test_omn_25342_contract_ac3_refuses_a_path_under_etc(repo: Path) -> None:
    _commit(repo, "etc/systemd/unit.conf")
    assert _run(_check("dod-omn25342-ac3-no-hand-edit"), repo) != 0


def test_omn_25342_contract_ac3_passes_a_clean_diff(repo: Path) -> None:
    _commit(repo, "src/a.py")
    _commit(repo, "tests/test_a.py")
    assert _run(_check("dod-omn25342-ac3-no-hand-edit"), repo) == 0


def test_omn_25342_contract_ac3_fails_when_the_merge_base_cannot_be_read(
    repo: Path,
) -> None:
    _git(repo, "update-ref", "-d", "refs/remotes/origin/dev")
    assert _run(_check("dod-omn25342-ac3-no-hand-edit"), repo) != 0


def test_omn_25342_contract_ac3_receipt_revision_must_equal_the_installed_unit(
    repo: Path, tmp_path: Path
) -> None:
    command = _check("dod-omn25342-ac3-receipt-matches-unit")
    matching = (
        "2026-11-17T16:00:00Z | STATUS | pr=omnimarket#6132 | installed=h201:abc123def456 | "
        "receipt=install-1 | receipt_rev=abc123def456 | read_at=2026-11-17T16:00:00Z"
    )
    differing = matching.replace("receipt_rev=abc123def456", "receipt_rev=000000000000")
    no_receipt = "2026-11-17T16:00:00Z | STATUS | pr=omnimarket#6132 | installed=h201:abc123def456"
    assert _run(command, repo, _ledger(tmp_path, matching)) == 0
    assert _run(command, repo, _ledger(tmp_path, differing)) != 0
    assert _run(command, repo, _ledger(tmp_path, no_receipt)) != 0
    assert _run(command, repo, _ledger(tmp_path, UNREACHABLE_ROW)) != 0


def test_omn_25342_contract_ac1_needs_read_at_and_this_heads_revision(
    repo: Path, tmp_path: Path
) -> None:
    head = _head(repo)
    command = _check("dod-omn25342-ac1-status-row")
    good = (
        f"2026-11-17T16:00:00Z | STATUS | pr=omnimarket#6132 | installed=h201:{head}0000 | "
        "observed=run line seen | read_at=2026-11-17T16:00:00Z"
    )
    no_read_at = good.replace(" | read_at=2026-11-17T16:00:00Z", "")
    other_revision = good.replace(head, "ffffffffffff")
    # The old check passes the "installed but unreachable" row, which names no read_at and no revision of this head.
    assert _run(_old_ac1(), repo, _ledger(tmp_path, UNREACHABLE_ROW)) == 0
    assert _run(command, repo, _ledger(tmp_path, UNREACHABLE_ROW)) != 0
    assert _run(command, repo, _ledger(tmp_path, no_read_at)) != 0
    assert _run(command, repo, _ledger(tmp_path, other_revision)) != 0
    assert _run(command, repo, _ledger(tmp_path, good)) == 0


def test_omn_25342_contract_ac2_needs_a_run_line_or_a_cause_and_an_actor_at_this_head(
    repo: Path, tmp_path: Path
) -> None:
    head = _head(repo)
    command = _check("dod-omn25342-ac2-run-line-or-cause")
    base = f"2026-11-17T16:00:00Z | STATUS | pr=omnimarket#6132 | head={head}"
    run_line = (
        f"{base} | run_line=2026-11-17T16:00:00Z node_dev_signed_in_read_effect handled"
    )
    cause = f"{base} | cause=the dev runtime has not rolled to this head | actor=dev deploy agent"
    cause_only = f"{base} | cause=the dev runtime has not rolled to this head"
    actor_only = f"{base} | actor=dev deploy agent"
    bare = f"{base} | installed=h201:{head}"
    old_head = run_line.replace(head, "ffffffffffff")
    assert _run(command, repo, _ledger(tmp_path, run_line)) == 0
    assert _run(command, repo, _ledger(tmp_path, cause)) == 0
    for refused in (cause_only, actor_only, bare, old_head, UNREACHABLE_ROW):
        assert _run(command, repo, _ledger(tmp_path, refused)) != 0, refused


def test_omn_25342_contract_every_pytest_command_refuses_skips() -> None:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    commands = [
        item["command"]
        for item in document["evidence_requirements"]
        if "command" in item
    ]
    commands += [
        check["check_value"]
        for entry in document["dod_evidence"]
        for check in entry["checks"]
    ]
    pytest_commands = [c for c in commands if " pytest " in c]
    assert len(pytest_commands) >= 4  # positive control: the scan found the commands
    for command in pytest_commands:
        assert "--refuse-skips" in command, command


def test_omn_25342_contract_refuse_skips_is_a_registered_option_and_asyncio_runs_auto(
    pytestconfig: pytest.Config,
) -> None:
    assert pytestconfig.getoption("--refuse-skips") is not None
    assert pytestconfig.getini("asyncio_mode") == "auto"


def test_omn_25342_contract_ac2_is_bound_to_a_check_that_reads_a_run_line_or_a_cause() -> (
    None
):
    bound = [e for e in _entries().values() if "AC2" in e.get("binds_ac", [])]
    assert [e["id"] for e in bound] == ["dod-omn25342-ac2-run-line-or-cause"]
