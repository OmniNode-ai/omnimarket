# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local Done gate checks of the alarm pull request's contract read what they claim (OMN-25071).

Each check is run as the gate runs it, with ``onex``, ``uv`` and the PR watcher reader replaced by shims over a temp
directory, so a comment for an old head, a short sha, a missing command or a missing exit 0 must fail.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

_REPO = Path(__file__).resolve().parents[3]
_CONTRACT = yaml.safe_load(
    (_REPO / "contracts" / "OMN-25071.yaml").read_text(encoding="utf-8")
)
_ITEMS = {item["id"]: item for item in _CONTRACT["dod_evidence"]}
_HEAD = "0123456789abcdef0123456789abcdef01234567"
_OLD = "89abcdef0123456789abcdef0123456789abcdef"
_LAB = "onex lab-work run --bus-lane dev --lane x --kind test -- uv run pytest"


class _World:
    """A temp directory holding the shims and the readings they print."""

    def __init__(self, root: Path) -> None:
        self.root = root
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True)
        self._script(
            bin_dir / "onex",
            '#!/bin/sh\ncase "$2" in\n'
            f'  pr-view) cat "{root}/comments.txt" ;;\n'
            f'  pr-checks) cat "{root}/checks.json" ;;\n'
            "  *) exit 2 ;;\nesac\n",
        )
        self._script(
            bin_dir / "uv",
            f'#!/bin/sh\nshift 3\nexec "{sys.executable}" -m pytest "$@"\n',
        )
        plugin = root / "plugin" / "skills" / "merge-drain" / "scripts"
        plugin.mkdir(parents=True)
        (plugin / "pr_state_local.py").write_text(
            f"import json\nprint(json.dumps({{'facts': {{'head_sha': '{_HEAD}'}}}}))\n",
            encoding="utf-8",
        )
        self.comments("")
        self.checks()

    @staticmethod
    def _script(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def comments(self, text: str) -> None:
        (self.root / "comments.txt").write_text(text, encoding="utf-8")

    def checks(
        self,
        *,
        head: str = _HEAD,
        red: tuple[str, ...] = (),
        pending: tuple[str, ...] = (),
        runs: int = 3,
    ) -> None:
        reading = {
            "result": {
                "head_sha": head,
                "red": list(red),
                "pending": list(pending),
                "cancelled": [],
                "runs": [["CI Summary", "completed", "success", "t"]] * runs,
            }
        }
        (self.root / "checks.json").write_text(json.dumps(reading), encoding="utf-8")

    def run(self, command: str, cwd: Path | None = None) -> int:
        env = {
            **scrub_git_location_env(os.environ),
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "OMNI_PLUGIN_ROOT": str(self.root / "plugin"),
        }
        return subprocess.run(
            command,
            shell=True,
            cwd=cwd or self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        ).returncode


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def _command(item: str, index: int) -> str:
    return str(_ITEMS[item]["checks"][index]["check_value"])


def _lab(head: str, *, command: str = _LAB, exit_text: str = "exit 0") -> str:
    return f"Lab: head={head}\nhost h053, command: {command}\nresult: {exit_text}\n"


@pytest.mark.unit
def test_ac6_lab_comment_check_passes_on_the_full_head_command_and_exit_zero(
    world: _World,
) -> None:
    world.comments(_lab(_HEAD))
    assert world.run(_command("dod-omn25071-ac6", 0)) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "comment",
    [
        "Lab: head=\nonly the prefix, no sha. exit 0\nonex lab-work run\n",
        _lab(_OLD),
        _lab(_HEAD[:12]),
        _lab(_HEAD, exit_text="exit 1"),
        _lab(_HEAD, exit_text="exit 10"),
        _lab(_HEAD, command="a local run"),
        _lab(_OLD) + _lab(_HEAD, exit_text="exit 1"),
        "",
    ],
    ids=[
        "prefix-only",
        "old-head",
        "short-sha",
        "exit-one",
        "exit-ten",
        "no-command",
        "old-comment-exit-zero-then-current-without",
        "no-comments",
    ],
)
def test_ac6_lab_comment_check_fails_without_the_current_full_head_command_or_exit_zero(
    world: _World, comment: str
) -> None:
    world.comments(comment)
    assert world.run(_command("dod-omn25071-ac6", 0)) != 0


@pytest.mark.unit
def test_ac6_pr_checks_check_reads_the_required_check_runs_at_the_head(
    world: _World,
) -> None:
    command = _command("dod-omn25071-ac6", 1)
    assert "onex scm pr-checks OmniNode-ai/omnimarket#6335" in command
    assert world.run(command) == 0
    world.checks(head=_OLD)
    assert world.run(command) != 0
    world.checks(red=("CI Summary",))
    assert world.run(command) != 0
    world.checks(pending=("lint",))
    assert world.run(command) != 0
    world.checks(runs=0)
    assert world.run(command) != 0


@pytest.mark.unit
def test_ac5_names_each_of_findings_5_to_8_with_its_own_check(world: _World) -> None:
    checks = _ITEMS["dod-omn25071-ac5"]["checks"]
    greps = [c["check_value"] for c in checks if "pr-view" in c["check_value"]]
    assert len(greps) == 4
    only_eight = "finding 8: argued with evidence\n"
    world.comments(only_eight)
    assert [world.run(g) == 0 for g in greps] == [False, False, False, True]
    world.comments("findings 5 to 8 are argued\nfinding 80 is another\n")
    assert [world.run(g) == 0 for g in greps] == [False] * 4
    world.comments("finding 5: a\nfinding 6: b\nfinding 7. c\nfinding 8\n")
    assert [world.run(g) == 0 for g in greps] == [True] * 4


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, env=scrub_git_location_env(os.environ))


def _repo(path: Path, files: dict[str, str]) -> None:
    _git("init", "-q", str(path))
    for name, text in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    ident = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    _git("-C", str(path), *ident, "add", "-A")
    _git("-C", str(path), *ident, "commit", "-q", "--allow-empty", "-m", "base")
    _git("-C", str(path), "update-ref", "refs/remotes/origin/dev", "HEAD")


@pytest.mark.unit
def test_ac3_and_ac4_check_that_the_cost_pr_test_files_are_on_dev_and_ac3_selects_a_test(
    tmp_path: Path,
) -> None:
    ac3_exists, ac3_selects = (
        _command("dod-omn25071-ac3", 0),
        _command("dod-omn25071-ac3", 1),
    )
    ac4_exists = _command("dod-omn25071-ac4", 0)
    absent = tmp_path / "absent"
    world = _World(tmp_path / "w1")
    _repo(absent, {"README.md": "x"})
    assert world.run(ac3_exists, cwd=absent) != 0
    assert world.run(ac4_exists, cwd=absent) != 0

    present = tmp_path / "present"
    _repo(
        present,
        {
            "tests/test_omn24874_contract_checks.py": "def test_ac3_reads():\n    pass\n",
            "contracts/OMN-24874.yaml": "ticket_id: OMN-24874\n",
            "tests/unit/delegation/test_omn24874_tenant_overlay_terminal_cost.py": "",
        },
    )
    assert world.run(ac3_exists, cwd=present) == 0
    assert world.run(ac4_exists, cwd=present) == 0
    assert world.run(ac3_selects, cwd=present) == 0

    none = tmp_path / "none"
    _repo(
        none,
        {"tests/test_omn24874_contract_checks.py": "def test_other():\n    pass\n"},
    )
    assert world.run(ac3_selects, cwd=none) != 0


@pytest.mark.unit
def test_the_contract_names_the_plain_contract_path() -> None:
    text = (_REPO / "contracts" / "OMN-25071.yaml").read_text(encoding="utf-8")
    assert "contracts/OMN-24874.yaml" in text
    assert "](https://" not in text
