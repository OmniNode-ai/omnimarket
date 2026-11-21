# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shell checks of contracts/OMN-25342.yaml can fail.

Each check string is read from the contract file and run with ``bash`` in a throwaway git repository, so what is
proved is the check the Done gate runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-25342.yaml"

# The checks as they stood at 51bdb423ac65.
OLD_AC3 = "! git diff --name-only HEAD~1 HEAD | grep -E '(^|/)(\\.service|\\.plist|\\.timer)$|^(etc|usr|Library)/'"

NODE_CONTRACT = "src/omnimarket/nodes/node_dev_signed_in_read_effect/contract.yaml"
COMMAND_TOPIC = "onex.cmd.omnimarket.dev-signed-in-read-requested.v1"


def _entries() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return {entry["id"]: entry for entry in document["dod_evidence"]}


def _check(entry_id: str) -> str:
    (check,) = _entries()[entry_id]["checks"]
    value: str = check["check_value"]
    return value


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


def _run(command: str, cwd: Path) -> int:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd)}
    return subprocess.run(
        ["bash", "-c", command], cwd=cwd, env=env, capture_output=True, text=True
    ).returncode


def _node_contract(*, subscribed: bool) -> str:
    """The node contract as the runtime reads it: the command topic is a subscribe_topics list item or it is not."""
    subscribe = (
        f"  subscribe_topics:\n    - {COMMAND_TOPIC}\n"
        if subscribed
        else "  subscribe_topics: []\n"
    )
    return f"topics:\n  command: {COMMAND_TOPIC}\nevent_bus:\n{subscribe}  publish_topics: []\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "work")
    (path / "README.md").write_text("base\n")
    (path / NODE_CONTRACT).parent.mkdir(parents=True)
    (path / NODE_CONTRACT).write_text(_node_contract(subscribed=False))
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


def test_omn_25342_contract_declares_the_interface_change_of_the_published_events() -> (
    None
):
    """The completed and failed topics are published for the first time and the result's realm may be null."""
    for name in ("OMN-25342.yaml", "OMN-25391.yaml"):
        document = yaml.safe_load((CONTRACT.parent / name).read_text(encoding="utf-8"))
        assert document["interface_change"] is True, name
        assert {"events", "topics"} <= set(document["interfaces_touched"]), name


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


def test_omn_25342_contract_local_done_gate_items_carry_no_binds_ac() -> None:
    local = [
        e for e in _entries().values() if e.get("execution_scope") == "local_done_gate"
    ]
    assert {e["id"] for e in local} >= {
        "dod-omn25342-integration-proof",
    }  # positive control: the scan found the lab item
    assert [e["id"] for e in local if e.get("binds_ac")] == []
