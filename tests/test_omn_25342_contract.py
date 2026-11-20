# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shell checks of contracts/OMN-25342.yaml can fail (reviews of the PR at 51bdb423ac65 and f852f8209e92).

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

NODE_CONTRACT = "src/omnimarket/nodes/node_dev_signed_in_read_effect/contract.yaml"
COMMAND_TOPIC = "onex.cmd.omnimarket.dev-signed-in-read-requested.v1"

# The checks as they stood at f852f8209e92 (the second review): string matches against the PR head, a bare ``cause=``.
OLD_AC1_F852 = (
    "rev=\"$(git rev-parse --short=12 HEAD)\" && grep -F 'pr=omnimarket#6132' \"$@LEDGER@\" | grep -F 'observed=' "
    "| grep -F 'read_at=' | grep -Eq \"installed=[^ |:]+:[^ |]*${rev}\""
)
OLD_AC2_F852 = (
    'rev="$(git rev-parse --short=12 HEAD)" && grep -F \'pr=omnimarket#6132\' "$@LEDGER@" | grep -F "${rev}" '
    "| grep -E 'run_line=[^ |]|cause=[^ |].*actor=[^ |]|actor=[^ |].*cause=[^ |]' | grep -q ."
)
OLD_RECEIPT_F852 = (
    "row=\"$(grep -F 'pr=omnimarket#6132' \"$@LEDGER@\" | grep -F 'receipt=' | tail -n 1)\" "
    "&& inst=\"$(printf '%s\\n' \"$row\" | sed -n 's/.*installed=[^ |:]*:\\([^ |]*\\).*/\\1/p')\" "
    "&& rcpt=\"$(printf '%s\\n' \"$row\" | sed -n 's/.*receipt_rev=\\([^ |]*\\).*/\\1/p')\" "
    '&& test -n "$inst" && test "$inst" = "$rcpt"'
)

# The cells every bus-written row carries (rows 2201 and 2671 of the ledger): the writer's actor and model cells are
# present on every row, so a check that reads ``actor=`` as "who must act" passes a row that names nobody.
BUS_PREFIX = (
    "2026-11-17T16:03:18Z | STATUS | lane=fix6372-1511 | principal=dev-jonah-hp-gen9 | ticket=OMN-25391 | "
    "actor=claude:sonnet:remote-lane | model=claude-sonnet-5-5 | parent=orchestrator-31d9-swarm | item=throughput | "
    "band=B3 | host=h051 | pr=omnimarket#6132"
)
BUS_SUFFIX = (
    " | req=39e96c9d-a93b-43d0-9dda-6561db8e73d8 | via=bus:h053 | text of the row"
)

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


def _old(check: str) -> str:
    """An old check with the ledger variable filled in from the current check text (the name is read, not spelled)."""
    return check.replace("@LEDGER@", _ledger_variable())


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


def _ledger(tmp_path: Path, *rows: str) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("".join(f"{row}\n" for row in rows))
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short=12", "HEAD")


def _base(repo: Path) -> str:
    return _git(repo, "rev-parse", "--short=12", "origin/dev")


def _pr_head(repo: Path) -> str:
    """The PR head: one commit on top of the base that subscribes the command topic. Returns its short revision."""
    (repo / NODE_CONTRACT).write_text(_node_contract(subscribed=True))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pr head")
    return _head(repo)


def _squash_merge(repo: Path) -> str:
    """What a squash merge does: a new commit on the base with the same tree change, never holding the PR head's sha."""
    pr_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _git(repo, "checkout", "-q", "-b", "dev-tip", "origin/dev")
    (repo / NODE_CONTRACT).write_text(_node_contract(subscribed=True))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "squash of the PR (#1)")
    squash = _head(repo)
    _git(repo, "update-ref", "refs/remotes/origin/dev", "HEAD")
    _git(repo, "checkout", "-q", pr_branch)
    return squash


def _row(*cells: str) -> str:
    return " | ".join([BUS_PREFIX, *cells]) + BUS_SUFFIX


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


def test_omn_25342_contract_ac1_needs_a_run_line_and_a_revision_that_holds_the_change(
    repo: Path, tmp_path: Path
) -> None:
    """The installed revision is judged by ancestry and content, not by matching the PR head's sha as a string.

    After a squash merge the host runs a commit that never holds the PR head's sha, so the old string match could
    not pass; and it passed ``observed=NOT RUNNING``. The new check wants a revision that descends from the merge
    base and whose node contract subscribes the command topic, plus a ``run_line=`` and a real observation.
    """
    pr_head = _pr_head(repo)
    base = _base(repo)
    squash = _squash_merge(repo)
    command = _check("dod-omn25342-ac1-status-row")
    stamp = "read_at=2026-11-17T18:30:00Z"
    good = _row(
        f"installed=h201:{squash}",
        "observed=the runtime log of h201 shows the node handled one command",
        "run_line=node_dev_signed_in_read_effect handled correlation 11111111",
        stamp,
    )
    on_pr_head = good.replace(squash, pr_head)
    not_running = good.replace(
        "observed=the runtime log", "observed=NOT RUNNING: the runtime log"
    )
    no_run_line = good.replace(
        " | run_line=node_dev_signed_in_read_effect handled correlation 11111111", ""
    )
    no_read_at = good.replace(f" | {stamp}", "")
    old_build = good.replace(
        squash, base
    )  # the dev runtime today: the merge base, which has no route
    # Controls: the check of f852f8209e92 refuses the squash revision (a string match on the PR head) and passes a
    # row that says NOT RUNNING and names the PR head.
    assert _run(_old(OLD_AC1_F852), repo, _ledger(tmp_path, good)) != 0
    assert (
        _run(
            _old(OLD_AC1_F852),
            repo,
            _ledger(tmp_path, not_running.replace(squash, pr_head)),
        )
        == 0
    )
    assert _run(command, repo, _ledger(tmp_path, good)) == 0
    assert _run(command, repo, _ledger(tmp_path, on_pr_head)) == 0
    for refused in (not_running, no_run_line, no_read_at, old_build, UNREACHABLE_ROW):
        assert _run(command, repo, _ledger(tmp_path, refused)) != 0, refused


def test_omn_25342_contract_ac1_refuses_a_revision_that_is_not_a_descendant_of_the_merge_base(
    repo: Path, tmp_path: Path
) -> None:
    _pr_head(repo)
    _git(repo, "checkout", "-q", "--orphan", "unrelated")
    (repo / "x.txt").write_text("x\n")
    (repo / NODE_CONTRACT).write_text(_node_contract(subscribed=True))
    _git(repo, "add", "-f", ".")
    _git(repo, "commit", "-q", "-m", "unrelated history that holds the subscription")
    unrelated = _head(repo)
    row = _row(
        f"installed=h201:{unrelated}",
        "observed=the runtime log of h201 shows the node handled one command",
        "run_line=node_dev_signed_in_read_effect handled correlation 11111111",
        "read_at=2026-11-17T18:30:00Z",
    )
    assert (
        _run(_check("dod-omn25342-ac1-status-row"), repo, _ledger(tmp_path, row)) != 0
    )


def test_omn_25342_contract_ac2_needs_a_run_line_or_a_cause_and_must_act_at_this_head(
    repo: Path, tmp_path: Path
) -> None:
    """Bus-written rows carry the writer's ``actor=`` cell, so ``actor=`` cannot say who must act: ``must_act=`` does."""
    head = _pr_head(repo)
    command = _check("dod-omn25342-ac2-run-line-or-cause")
    run_line = _row(
        f"head={head}",
        "run_line=2026-11-17T18:30:00Z node_dev_signed_in_read_effect handled",
    )
    cause = _row(
        f"head={head}",
        "cause=the dev runtime has not rolled to this head",
        "must_act=the dev deploy agent rolls h201",
    )
    cause_only = _row(
        f"head={head}", "cause=the dev runtime has not rolled to this head"
    )
    cause_must_act_first = _row(
        f"head={head}",
        "must_act=the dev deploy agent rolls h201",
        "cause=the dev runtime has not rolled to this head",
    )
    must_act_only = _row(f"head={head}", "must_act=the dev deploy agent rolls h201")
    bare = _row(f"head={head}", f"installed=h201:{head}")
    old_head = _row(
        "head=ffffffffffff",
        "run_line=2026-11-17T18:30:00Z node_dev_signed_in_read_effect handled",
    )
    sha_in_text_only = _row(
        "head=ffffffffffff",
        f"cause=waiting on {head}",
        "must_act=the dev deploy agent rolls h201",
    )
    # Control: the check of f852f8209e92 passes a bus-written row whose only cause cell is ``cause=`` (the writer's
    # ``actor=`` cell is in every row) and passes the head's sha found anywhere in the text.
    assert _run(_old(OLD_AC2_F852), repo, _ledger(tmp_path, cause_only)) == 0
    assert _run(_old(OLD_AC2_F852), repo, _ledger(tmp_path, sha_in_text_only)) == 0
    assert _run(command, repo, _ledger(tmp_path, run_line)) == 0
    assert _run(command, repo, _ledger(tmp_path, cause)) == 0
    assert _run(command, repo, _ledger(tmp_path, cause_must_act_first)) == 0
    for refused in (
        cause_only,
        must_act_only,
        bare,
        old_head,
        sha_in_text_only,
        UNREACHABLE_ROW,
    ):
        assert _run(command, repo, _ledger(tmp_path, refused)) != 0, refused


def test_omn_25342_contract_ac3_receipt_check_requires_a_revision_that_holds_the_change(
    repo: Path, tmp_path: Path
) -> None:
    """Two hand-typed cells that agree are not a receipt of this head: the revision must hold the change."""
    pr_head = _pr_head(repo)
    base = _base(repo)
    squash = _squash_merge(repo)
    command = _check("dod-omn25342-ac3-receipt-matches-unit")
    matching = _row(
        f"installed=h201:{squash}", "receipt=install-1", f"receipt_rev={squash}"
    )
    on_pr_head = _row(
        f"installed=h201:{pr_head}", "receipt=install-1", f"receipt_rev={pr_head}"
    )
    differing = _row(
        f"installed=h201:{squash}", "receipt=install-1", f"receipt_rev={base}"
    )
    no_receipt = _row(f"installed=h201:{squash}")
    # The fixture of the review: both cells agree on the merge base, the build the dev runtime runs today.
    both_on_the_old_build = _row(
        f"installed=h201:{base}", "receipt=install-1", f"receipt_rev={base}"
    )
    installer_receipt_only = _row(
        f"installed=h201:{squash}", f"installer_receipt=read of the agent, rev {squash}"
    )
    assert (
        _run(_old(OLD_RECEIPT_F852), repo, _ledger(tmp_path, both_on_the_old_build))
        == 0
    )  # the control
    assert _run(command, repo, _ledger(tmp_path, matching)) == 0
    assert _run(command, repo, _ledger(tmp_path, on_pr_head)) == 0
    for refused in (
        differing,
        no_receipt,
        both_on_the_old_build,
        installer_receipt_only,
        UNREACHABLE_ROW,
    ):
        assert _run(command, repo, _ledger(tmp_path, refused)) != 0, refused


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
        "dod-omn25342-ac2-run-line-or-cause",
        "dod-omn25342-integration-proof",
    }  # positive control: the scan found the ledger and lab items
    assert [e["id"] for e in local if e.get("binds_ac")] == []
