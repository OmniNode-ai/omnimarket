# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shell checks of this change's ticket contracts, run over fixtures with the lab host and the watcher faked.

The contracts bind the floor fill pull request's criteria to commands that read a lab host's ledger, its alarm check log
and the pull request's checks. Each command is run here with ``ssh``, ``onex lab-work run``, ``onex scm pr-view`` and
``onex scm pr-checks`` replaced by scripts that answer from fixtures, so a command that matches the alarm's own rows as
CLAIMs, reads the fill's history for the alarm's run, never compares the installed revision to the head or passes on one
success among failures fails here.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.floor_fill_proof_readers import (
    ProofMissingError,
    lab_head,
    no_handoff,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[5]
FAKE_ONEX = """#!/bin/sh
case "$1 $2" in
  "lab-work run")
    while [ "$1" != "--" ]; do shift; done
    shift
    exec "$@"
    ;;
  "scm pr-view")
    printf '{"result": {"head_sha": "%s"}}\\n' "$FIXTURE_WATCHER_HEAD"
    ;;
  "scm pr-checks")
    cat "$FIXTURE_CHECKS"
    ;;
  *)
    echo "unexpected onex call: $*" >&2
    exit 2
    ;;
esac
"""
FAKE_PR_STATE = """import os
import sys

sys.stdout.write(open(os.environ["FIXTURE_PR_JSON"], encoding="utf-8").read())
"""
FAKE_SSH = '#!/bin/sh\nshift\nexec sh -c "$1"\n'
ALARM_ROW = "2026-11-16T10:00:00Z | STATUS | lane=floor-fill-alarm | actor=script:floor-fill-alarm-check | model=none | ticket=OMN-24873 | alarm=floor-fill-unclaimed | alarm-key=abc | lane x no CLAIM key=floor:a:b 12 minutes"


def _head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _checks(contract: str, dod_id: str) -> list[str]:
    document = yaml.safe_load(
        (REPO / "contracts" / f"{contract}.yaml").read_text(encoding="utf-8")
    )
    for entry in document["dod_evidence"]:
        if entry["id"] == dod_id:
            return [check["check_value"] for check in entry["checks"]]
    raise AssertionError(f"{dod_id} is not in {contract}")


class _Host:
    """A fake lab host: its ledger, its alarm state directory, and the watcher's answers."""

    def __init__(self, root: Path) -> None:
        self.root = root
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name, body in (("onex", FAKE_ONEX), ("ssh", FAKE_SSH)):
            script = bin_dir / name
            script.write_text(body, encoding="utf-8")
            script.chmod(0o755)
        self.home = root / "home"
        (self.home / "docs" / "tracking").mkdir(parents=True)
        (self.home / ".onex_state" / "floor-fill-alarm").mkdir(parents=True)
        # The ledger file the checks read, named by the check itself (its path under $OMNI_HOME).
        named = re.search(
            r"\$OMNI_HOME/(docs/tracking/\S+?\.md)",
            _checks("OMN-24873", "dod-omn24873-ac5-integration-proof")[2],
        )
        assert named is not None
        self.ledger = self.home / named.group(1)
        scripts = root / "plugin" / "skills" / "merge-drain" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "pr_state_local.py").write_text(FAKE_PR_STATE, encoding="utf-8")
        self.pr_file = root / "pr.json"
        self.pr_file.write_text("", encoding="utf-8")
        self.pr_state = ""
        self.checks_file = root / "checks.txt"
        self.checks_file.write_text("", encoding="utf-8")
        self.watcher_head = _head()

    def ledger_env(self, command: str) -> dict[str, str]:
        """The variable the check names for its ledger file (read from the check itself), set to this host's file."""
        named = re.search(r'test -s "\$(\w+)"', command)
        assert named is not None
        return {named.group(1): str(self.ledger)}

    @property
    def pr_json(self) -> str:
        return self.pr_file.read_text(encoding="utf-8")

    @pr_json.setter
    def pr_json(self, text: str) -> None:
        self.pr_file.write_text(text, encoding="utf-8")

    def run(self, command: str, **extra_env: str) -> int:
        env = {
            **os.environ,
            **extra_env,
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "OMNI_HOME": str(self.home),
            "FIXTURE_CHECKS": str(self.checks_file),
            "FIXTURE_PR_JSON": str(self.pr_file),
            "OMNI_PLUGIN_ROOT": str(self.root / "plugin"),
            "FIXTURE_WATCHER_HEAD": self.watcher_head,
            "TMPDIR": str(self.root),
        }
        return subprocess.run(
            ["sh", "-c", command],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        ).returncode


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(tmp_path)


def _claim(lane: str, key: str, stamp: str = "2026-11-16T10:06:00Z") -> str:
    return f"{stamp} | CLAIM | lane={lane} | key={key} | ticket=OMN-1"


def _install_row(head: str, stamp: str = "2026-11-16T10:05:00Z") -> str:
    return f"{stamp} | STATUS | lane=l | ticket=OMN-24873 | installed=h051:{head[:12]} | observed=2 runs, 2 lanes"


AC5 = "dod-omn24873-ac5-integration-proof"


def test_floor_fill_alarm_contract_claim_check_counts_claim_rows_not_the_alarm_rows(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", AC5)[2]
    install = _install_row(_head())
    host.ledger.write_text(
        "\n".join(
            [
                install,
                ALARM_ROW,
                ALARM_ROW.replace("key=floor:a:b", "key=floor:a:c"),
                _claim("l1", "floor:a:b"),
            ]
        ),
        encoding="utf-8",
    )
    assert host.run(command) != 0, "one CLAIM and two alarm rows are not two CLAIMs"
    host.ledger.write_text(
        "\n".join(
            [install, ALARM_ROW, _claim("l1", "floor:a:b"), _claim("l2", "floor:a:c")]
        ),
        encoding="utf-8",
    )
    assert host.run(command) == 0, "two CLAIM rows pass beside an alarm row"


def test_floor_fill_alarm_contract_claims_of_an_older_revision_do_not_count(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", AC5)[2]
    old = "2026-11-16T09:00:00Z"
    host.ledger.write_text(
        "\n".join(
            [
                _claim("l1", "floor:a:b", old),
                _claim("l2", "floor:a:c", old),
                _install_row(_head()),
            ]
        ),
        encoding="utf-8",
    )
    assert host.run(command) != 0, "two CLAIMs before the install are not the head's"
    host.ledger.write_text(
        "\n".join(
            [
                _claim("l1", "floor:a:b", old),
                _install_row(_head()),
                _claim("l2", "floor:a:c"),
                _claim("l3", "floor:a:d"),
            ]
        ),
        encoding="utf-8",
    )
    assert host.run(command) == 0


def test_floor_fill_alarm_contract_a_missing_ledger_file_fails_every_ledger_check(
    host: _Host,
) -> None:
    claims = _checks("OMN-24873", AC5)[2]
    checks = _checks("OMN-24873", AC5)[3]
    handoff = _checks("OMN-24873", "dod-omn24873-ac6-lab-line")[1]
    assert not host.ledger.exists()
    assert host.run(claims) != 0
    assert host.run(checks) != 0
    assert host.run(handoff, **host.ledger_env(handoff)) != 0


def test_floor_fill_alarm_contract_two_consecutive_sending_runs_are_read(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", AC5)[1]
    state = host.home / ".onex_state" / "floor-fill-alarm"

    def line(seq: int, sent: int) -> str:
        return json.dumps({"seq": seq, "run": {"lanes_sent": sent}})

    assert host.run(command) != 0, "no run log is not two runs"
    runs = state / "runs.jsonl"
    runs.write_text("\n".join([line(1, 2), line(3, 2)]) + "\n", encoding="utf-8")
    assert host.run(command) != 0, "runs 1 and 3 are not consecutive"
    runs.write_text("\n".join([line(1, 2), line(2, 0)]) + "\n", encoding="utf-8")
    assert host.run(command) != 0, "a run that sent nothing is not a sending run"
    runs.write_text(
        "\n".join([line(1, 0), line(2, 1), line(3, 3)]) + "\n", encoding="utf-8"
    )
    assert host.run(command) == 0


def test_floor_fill_alarm_contract_alarm_run_check_reads_the_check_log_not_the_fill_history(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", AC5)[3]
    state = host.home / ".onex_state" / "floor-fill-alarm"
    host.ledger.write_text(_install_row(_head()) + "\n", encoding="utf-8")
    (state / "runs.jsonl").write_text('{"seq": 1, "run": {}}\n', encoding="utf-8")
    assert host.run(command) != 0, "the fill history alone is not an alarm run"
    checks = state / "checks.jsonl"
    checks.write_text(
        '{"alarms_found": 0, "outcome": "checked", "at": "2026-11-16T10:00:00+00:00"}\n',
        encoding="utf-8",
    )
    assert host.run(command) != 0, (
        "a check line from before the install is another revision's"
    )
    checks.write_text(
        '{"alarms_found": 0, "outcome": "failed", "at": "2026-11-16T10:30:00+00:00"}\n',
        encoding="utf-8",
    )
    assert host.run(command) != 0, "a failed check is not a check that ran"
    checks.write_text(
        '{"alarms_found": 0, "outcome": "checked", "at": "2026-11-16T10:30:00+00:00"}\n',
        encoding="utf-8",
    )
    assert host.run(command) == 0


def test_floor_fill_alarm_contract_the_install_row_must_be_the_head(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", AC5)[2]
    head = _head()
    other = "0" * 12 if head[:12] != "0" * 12 else "1" * 12
    body = [_claim("l1", "floor:a:b"), _claim("l2", "floor:a:c")]
    host.ledger.write_text("\n".join([_install_row(other), *body]), encoding="utf-8")
    assert host.run(command) != 0, "a row installed at another revision is not the head"
    host.ledger.write_text("\n".join([_install_row(head), *body]), encoding="utf-8")
    assert host.run(command) == 0


def test_floor_fill_alarm_contract_the_lab_line_sha_is_compared_to_the_head(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", "dod-omn24873-ac6-lab-line")[0]
    template = 'source: x\n{"facts": {"body": "text\\nLab: head=SHA\\nmore"}}'
    for sha, passes in ((_head(), True), ("f" * 40, False), ("abc123", False)):
        host.pr_json = template.replace("SHA", sha)
        assert (host.run(command) == 0) is passes, sha
    host.pr_json = "source: x\n{}"
    assert host.run(command) != 0, "a body with no Lab: line fails"


def test_floor_fill_alarm_contract_the_lab_sha_may_be_an_ancestor_but_not_another_branch(
    tmp_path: Path,
) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "first")
    first = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "second")
    git("checkout", "-q", "-b", "other", first)
    git("commit", "-q", "--allow-empty", "-m", "elsewhere")
    elsewhere = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    body = tmp_path / "pr.json"

    def check(sha: str) -> int:
        body.write_text(f'{{"body": "x\\nLab: head={sha}"}}', encoding="utf-8")
        try:
            lab_head(body, tmp_path)
        except ProofMissingError:
            return 1
        return 0

    assert check(git("rev-parse", "HEAD")) == 0
    assert check(first) == 0, "the body's sha is the head of its creation: an ancestor"
    assert check(elsewhere) == 1, "a commit of another branch is not this head"


def test_floor_fill_alarm_contract_the_handoff_reader_fails_on_a_missing_ledger_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(ProofMissingError, match="unreadable"):
        no_handoff(tmp_path / "absent.md", "omnimarket#6333")
    present = tmp_path / "ledger.md"
    present.write_text(ALARM_ROW + "\n", encoding="utf-8")
    no_handoff(present, "omnimarket#6333")


def test_floor_fill_alarm_contract_the_handoff_check_reads_the_ledger_file(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", "dod-omn24873-ac6-lab-line")[1]
    env = host.ledger_env(command)
    host.ledger.write_text(ALARM_ROW + "\n", encoding="utf-8")
    assert host.run(command, **env) == 0
    host.ledger.write_text(
        "2026-11-16T10:00:00Z | MSG | from=l | to=landing-controller | pr=OmniNode-ai/omnimarket#6333 | head=x | needs=land\n",
        encoding="utf-8",
    )
    assert host.run(command, **env) != 0
    host.ledger.write_text("", encoding="utf-8")
    assert host.run(command, **env) != 0, "an empty ledger file proves nothing"


def _pr_checks_commands() -> tuple[str, str]:
    head_check, checks_check = _checks("OMN-24966", "dod-omn24966-ac10")
    return head_check, checks_check


def test_floor_fill_alarm_contract_pr_checks_fail_on_any_non_success(
    host: _Host,
) -> None:
    _head_check, checks_check = _pr_checks_commands()
    host.checks_file.write_text(
        "lint success\ntests success\nbuild success\n", encoding="utf-8"
    )
    assert host.run(checks_check) == 0
    host.checks_file.write_text(
        "lint success\ntests failure\nbuild success\n", encoding="utf-8"
    )
    assert host.run(checks_check) != 0, "one failure among successes fails"
    host.checks_file.write_text("lint success\ntests pending\n", encoding="utf-8")
    assert host.run(checks_check) != 0, "a pending check is not success"
    host.checks_file.write_text("", encoding="utf-8")
    assert host.run(checks_check) != 0, "no check read is not success"


def test_floor_fill_alarm_contract_pr_head_must_be_this_checkout(host: _Host) -> None:
    head_check, _checks_check = _pr_checks_commands()
    assert host.run(head_check) == 0
    host.watcher_head = "f" * 40 if _head() != "f" * 40 else "e" * 40
    assert host.run(head_check) != 0, "a watcher head that is not this head fails"


def test_floor_fill_alarm_contract_the_cause_comment_is_read_by_id() -> None:
    (command,) = _checks("OMN-24873", "dod-omn24873-ac1-cause-comment")
    assert 'comment(id: \\"a3691fd6-32c8-45c1-b271-46c8e322fbee\\")' in command
    assert "onex ticket read" not in command, "the whole ticket is not what is grepped"


def test_floor_fill_alarm_contract_unmet_fill_order_runs_only_at_the_done_gate() -> (
    None
):
    documents: dict[str, Any] = {
        name: yaml.safe_load(
            (REPO / "contracts" / f"{name}.yaml").read_text(encoding="utf-8")
        )
        for name in ("OMN-24873", "OMN-24906")
    }
    for name, document in documents.items():
        (entry,) = [
            e for e in document["dod_evidence"] if "fill_order" in str(e["checks"])
        ]
        assert entry["execution_scope"] == "local_done_gate", name
