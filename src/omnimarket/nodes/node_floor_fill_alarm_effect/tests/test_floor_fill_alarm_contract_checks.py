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

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

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
            _checks("OMN-24873", "dod-omn24873-ac5-integration-proof")[1],
        )
        assert named is not None
        self.ledger = self.home / named.group(1)
        self.checks_file = root / "checks.txt"
        self.checks_file.write_text("", encoding="utf-8")
        self.watcher_head = _head()

    def run(self, command: str) -> int:
        env = {
            **os.environ,
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "OMNI_HOME": str(self.home),
            "FIXTURE_CHECKS": str(self.checks_file),
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


def _claim(lane: str, key: str) -> str:
    return f"2026-11-16T10:01:00Z | CLAIM | lane={lane} | key={key} | ticket=OMN-1"


def test_floor_fill_alarm_contract_claim_check_counts_claim_rows_not_the_alarm_rows(
    host: _Host,
) -> None:
    (command,) = [
        _checks("OMN-24873", "dod-omn24873-ac5-integration-proof")[1],
    ]
    host.ledger.write_text(
        "\n".join(
            [
                ALARM_ROW,
                ALARM_ROW.replace("key=floor:a:b", "key=floor:a:c"),
                _claim("l1", "floor:a:b"),
            ]
        ),
        encoding="utf-8",
    )
    assert host.run(command) != 0, "one CLAIM and two alarm rows are not two CLAIMs"
    host.ledger.write_text(
        "\n".join([ALARM_ROW, _claim("l1", "floor:a:b"), _claim("l2", "floor:a:c")]),
        encoding="utf-8",
    )
    assert host.run(command) == 0, "two CLAIM rows pass beside an alarm row"


def test_floor_fill_alarm_contract_alarm_run_check_reads_the_check_log_not_the_fill_history(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", "dod-omn24873-ac5-integration-proof")[2]
    state = host.home / ".onex_state" / "floor-fill-alarm"
    (state / "runs.jsonl").write_text('{"seq": 1, "run": {}}\n', encoding="utf-8")
    assert host.run(command) != 0, "the fill history alone is not an alarm run"
    (state / "checks.jsonl").write_text(
        '{"alarms_found": 0, "outcome": "checked"}\n', encoding="utf-8"
    )
    assert host.run(command) == 0


def test_floor_fill_alarm_contract_installed_revision_must_be_the_head(
    host: _Host,
) -> None:
    command = _checks("OMN-24873", "dod-omn24873-ac5-integration-proof")[3]
    head = _head()
    other = "0" * 12 if head[:12] != "0" * 12 else "1" * 12
    base = "2026-11-16T10:05:00Z | STATUS | lane=l | ticket=OMN-24873 | "
    host.ledger.write_text(
        base + f"installed=h051:{other} | observed=2 runs, 2 lanes\n", encoding="utf-8"
    )
    assert host.run(command) != 0, "a row installed at another revision is not the head"
    host.ledger.write_text(
        base + f"installed=h051:{head[:12]} | observed=2 runs, 2 lanes\n",
        encoding="utf-8",
    )
    assert host.run(command) == 0


def _pr_checks_commands() -> tuple[str, str]:
    head_check, checks_check = _checks("OMN-24906", "dod-omn24906-ac5")
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
