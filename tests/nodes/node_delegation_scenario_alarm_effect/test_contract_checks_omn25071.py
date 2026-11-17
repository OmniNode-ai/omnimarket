# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local Done gate checks of the alarm pull request's contracts read what they claim (OMN-25071, OMN-25206, OMN-25281).

Each check is run as the gate runs it, with ``onex``, ``uv`` and the PR watcher reader replaced by shims over a temp
directory. The shims print the shapes the real tools print: ``onex scm pr-view --source bus`` prints the typed answer
with ``result.comments`` (it has no ``--comments`` option, and without ``--source bus`` the watcher answers with no
comments at all), ``onex scm pr-checks`` prints the watcher's reading unless the bus answers, and the PR watcher reader
prints ``facts.head_sha``. A comment for an old head, a short sha, a missing command or exit 0, a quoted review, or a
shape the real tool never prints must fail.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from _pytest.mark.expression import Expression
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.model_github_gateway_io import (
    ModelCiChecksResult,
    ModelPrComment,
    ModelPrTextResult,
)
from omnimarket.models.scm.model_scm_command import (
    EnumScmNode,
    ModelScmAnswer,
    ModelScmBusReceipt,
)

_REPO = Path(__file__).resolve().parents[3]
_CONTRACTS = {
    name: yaml.safe_load(
        (_REPO / "contracts" / f"{name}.yaml").read_text(encoding="utf-8")
    )
    for name in ("OMN-25071", "OMN-25206", "OMN-25281")
}
_HEAD = "0123456789abcdef0123456789abcdef01234567"
_OLD = "89abcdef0123456789abcdef0123456789abcdef"
_LAB = "onex lab-work run --bus-lane dev --lane x --kind test -- uv run pytest"
# The top-level keys and the result keys the real tools printed on 2026-11-17 for an open pull request.
_REAL_STATE_KEYS = frozenset(
    {
        "baseline",
        "blocked",
        "bus_closed_emitted",
        "caller_fields",
        "ci",
        "cls",
        "facts",
        "first_seen",
        "handoff",
        "queue",
        "queued",
        "reason",
        "review",
    }
)
_REAL_WATCHER_CHECK_KEYS = frozenset(
    {
        "repo",
        "number",
        "head_sha",
        "class",
        "read_at",
        "red",
        "pending",
        "cancelled",
        "red_facts",
        "runs",
    }
)


def _all_items() -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for contract in _CONTRACTS.values()
        for item in contract["dod_evidence"]
    }


_ITEMS = _all_items()


class _World:
    """A temp directory holding the shims and the readings they print."""

    def __init__(self, root: Path) -> None:
        self.root = root
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True)
        self._script(
            bin_dir / "onex",
            '#!/bin/sh\ncase "$*" in *--comments*) echo "Error: No such option: --comments" >&2; exit 2 ;; esac\n'
            'case "$2" in\n'
            f'  pr-view) case "$*" in *"--source bus"*) cat "{root}/comments.json" ;; *) cat "{root}/view.json" ;; esac ;;\n'
            f'  pr-checks) case "$*" in *"--source bus"*) cat "{root}/checks-bus.json" ;; *) cat "{root}/checks.json" ;; esac ;;\n'
            "  *) exit 2 ;;\nesac\n",
        )
        self._script(
            bin_dir / "uv",
            f'#!/bin/sh\nshift 3\nexec "{sys.executable}" -m pytest "$@"\n',
        )
        plugin = root / "plugin" / "skills" / "merge-drain" / "scripts"
        plugin.mkdir(parents=True)
        self.state_script = plugin / "pr_state_local.py"
        self.state()
        self.comments()
        self.checks()
        (root / "view.json").write_text(self._answer("pr-view", {}), encoding="utf-8")

    @staticmethod
    def _script(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    @staticmethod
    def _answer(verb: str, result: dict[str, object], *, bus: bool = False) -> str:
        receipt = (
            ModelScmBusReceipt(
                correlation_id=UUID(int=1),
                command_topic="onex.cmd.x.v1",
                command_envelope_id=UUID(int=2),
                terminal_topic="onex.evt.x.v1",
            )
            if bus
            else None
        )
        return ModelScmAnswer(
            verb=verb,
            node=EnumScmNode.GATEWAY,
            operation="pr_text" if verb == "pr-view" else "ci_checks",
            repo="OmniNode-ai/omnimarket",
            source="bus" if bus else "watcher",
            outcome="completed",
            watcher_tick=None if bus else "2026-11-17T07:00:00Z",
            bus=receipt,
            result=result,
        ).model_dump_json()

    def state(self, *, head: str = _HEAD, shape: str = "real") -> None:
        """The PR watcher reader's ``--json`` answer: ``facts.head_sha`` among the real top-level keys."""
        document: dict[str, object] = dict.fromkeys(_REAL_STATE_KEYS)
        document["facts"] = {"head_sha": head, "number": 6353, "state": "OPEN"}
        if shape == "flat":
            document = {"head_sha": head}
        self.state_script.write_text(
            f"import json\nprint(json.dumps({document!r}))\n",
            encoding="utf-8",
        )

    def comments(
        self, *bodies: str | tuple[str, str], raw: str | None = None, head: str = _HEAD
    ) -> None:
        """The comments the bus read prints, newest last unless a body carries its own ``created_at``."""
        comments = []
        for index, entry in enumerate(bodies):
            body, created = (
                entry
                if isinstance(entry, tuple)
                else (entry, f"2026-11-17T07:{index:02d}:00Z")
            )
            comments.append(
                ModelPrComment(
                    id=index + 1,
                    author="lane",
                    created_at=created,
                    updated_at=created,
                    body=body,
                    body_truncated=False,
                )
            )
        text = ModelPrTextResult(
            repo="OmniNode-ai/omnimarket",
            pr_number=6353,
            title="t",
            body="",
            body_truncated=False,
            state="OPEN",
            is_draft=True,
            merged=False,
            author="a",
            head_ref="r",
            head_sha=head,
            base_ref="dev",
            truncated=False,
            comments=comments,
        )
        (self.root / "comments.json").write_text(
            raw
            if raw is not None
            else self._answer("pr-view", text.model_dump(mode="json"), bus=True),
            encoding="utf-8",
        )

    def checks(
        self,
        *,
        head: str = _HEAD,
        red: tuple[str, ...] = (),
        pending: tuple[str, ...] = (),
        cancelled: tuple[str, ...] = (),
        runs: int = 3,
        shape: str = "real",
    ) -> None:
        reading: dict[str, object] = {
            "repo": "OmniNode-ai/omnimarket",
            "number": 6335,
            "head_sha": head,
            "class": "green",
            "read_at": "2026-11-17T07:00:00Z",
            "red": list(red),
            "pending": list(pending),
            "cancelled": list(cancelled),
            "red_facts": [],
            "runs": [["CI Summary", "completed", "success", "t"]] * runs,
        }
        assert set(reading) == _REAL_WATCHER_CHECK_KEYS
        document = self._answer("pr-checks", reading)
        if shape == "flat":
            document = json.dumps({**reading})
        (self.root / "checks.json").write_text(document, encoding="utf-8")
        bus_result = ModelCiChecksResult(
            repo="OmniNode-ai/omnimarket",
            pr_number=6335,
            overall="pending",
            total=1,
            passed=0,
            failed=0,
            pending=1,
        ).model_dump(mode="json")
        (self.root / "checks-bus.json").write_text(
            self._answer("pr-checks", bus_result, bus=True), encoding="utf-8"
        )

    def run(self, command: str, cwd: Path | None = None) -> int:
        env = {
            **scrub_git_location_env(os.environ),
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "OMNI_PLUGIN_ROOT": str(self.root / "plugin"),
        }
        return subprocess.run(
            command,
            shell=True,
            cwd=cwd or _REPO,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        ).returncode


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


def _commands(item: str, contains: str) -> list[str]:
    checks = _ITEMS[item]["checks"]
    return [str(c["check_value"]) for c in checks if contains in str(c["check_value"])]


def _one(item: str, contains: str) -> str:
    (command,) = _commands(item, contains)
    return command


def _lab(head: str, *, command: str = _LAB, exit_text: str = "exit 0") -> str:
    return f"Lab: head={head}\nhost h053, command: {command}\nresult: {exit_text}\n"


# Each contract's Lab check and its per-number check, as the gate runs them (one comment of the pull request).
_LAB_CHECKS = [
    ("dod-omn25071-ac6", "--lab"),
    ("dod-omn25206-ac4", "--lab"),
    ("dod-omn25281-ac6", "--lab"),
]
_FINDING_CHECKS = [
    ("dod-omn25071-ac5", "--finding"),
    ("dod-omn25206-ac4", "--finding"),
    ("dod-omn25281-ac6", "--finding"),
]


def _naming(numbers: Sequence[int]) -> str:
    return "\n".join(f"finding {n}: argued with evidence" for n in numbers)


def _numbers(command: str) -> list[int]:
    return [int(n) for n in re.findall(r"--finding (\d+)", command)]


@pytest.mark.unit
@pytest.mark.parametrize(("item", "marker"), _LAB_CHECKS)
def test_lab_comment_check_passes_on_the_full_head_command_and_exit_zero(
    world: _World, item: str, marker: str
) -> None:
    world.comments(_lab(_HEAD))
    assert world.run(_one(item, marker)) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "comments",
    [
        ["Lab: head=\nonly the prefix, no sha. exit 0\nonex lab-work run\n"],
        [_lab(_OLD)],
        [_lab(_HEAD[:12])],
        [_lab(_HEAD, exit_text="exit 1")],
        [_lab(_HEAD, exit_text="exit 10")],
        [_lab(_HEAD, command="a local run")],
        [_lab(_OLD), _lab(_HEAD, exit_text="exit 1")],
        [],
        # AC2: a failing marker comment followed by a later comment holding the command and exit 0 (and no marker).
        [
            _lab(_HEAD, command="a local run", exit_text="exit 1"),
            f"later note: {_LAB}\nexit 0\n",
        ],
        # The command and the exit words live in a comment for the old head that follows the current marker.
        [_lab(_HEAD, command="a local run", exit_text="exit 1"), _lab(_OLD)],
        # A good comment, then a newer Lab comment for the same head that reads a failure: the newest is read.
        [_lab(_HEAD), _lab(_HEAD, exit_text="exit 1")],
        # The marker is not on the first line of the comment.
        [f"see below\n{_lab(_HEAD)}"],
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
        "failing-marker-then-later-comment-with-command-and-exit-zero",
        "failing-marker-then-old-head-comment-with-command-and-exit-zero",
        "newest-lab-comment-for-the-head-fails",
        "marker-not-first-line",
    ],
)
@pytest.mark.parametrize(("item", "marker"), _LAB_CHECKS)
def test_lab_comment_check_fails_without_the_current_full_head_command_or_exit_zero(
    world: _World, item: str, marker: str, comments: list[str]
) -> None:
    world.comments(*comments)
    assert world.run(_one(item, marker)) != 0


@pytest.mark.unit
@pytest.mark.parametrize(("item", "marker"), _LAB_CHECKS)
def test_a_corrected_second_lab_comment_for_the_head_is_the_one_read(
    world: _World, item: str, marker: str
) -> None:
    command = _one(item, marker)
    world.comments(_lab(_HEAD, exit_text="exit 1"), _lab(_HEAD))
    assert world.run(command) == 0
    # The newest is read by its created_at, not by its place in the list.
    world.comments(
        (_lab(_HEAD), "2026-11-17T08:00:00Z"),
        (_lab(_HEAD, exit_text="exit 1"), "2026-11-17T07:00:00Z"),
    )
    assert world.run(command) == 0


@pytest.mark.unit
@pytest.mark.parametrize(("item", "marker"), _FINDING_CHECKS)
def test_each_finding_number_is_read_inside_the_fixer_comment_for_the_head(
    world: _World, item: str, marker: str
) -> None:
    command = _one(item, marker)
    numbers = _numbers(command)
    assert len(numbers) >= 4
    review = f"VERDICT CHANGES NEEDED head={_HEAD}\n{_naming(numbers)}\n"
    # AC1: a review that quotes every number, and no fixer comment for the head, passes nothing.
    world.comments(review)
    assert world.run(command) != 0
    world.comments(review, _lab(_OLD) + _naming(numbers))
    assert world.run(command) != 0
    # A fixer comment that names all but one number fails, even though the review names every number.
    for skipped in numbers:
        world.comments(
            review,
            f"Lab: head={_HEAD}\n"
            + _naming([n for n in numbers if n != skipped])
            + "\n",
        )
        assert world.run(command) != 0
    # A number that is a prefix of another does not read as that number.
    world.comments(f"Lab: head={_HEAD}\n" + _naming([n * 10 for n in numbers]))
    assert world.run(command) != 0
    world.comments(review, f"Lab: head={_HEAD}\n{_naming(numbers)}\n")
    assert world.run(command) == 0


@pytest.mark.unit
@pytest.mark.parametrize(("item", "marker"), _FINDING_CHECKS)
def test_the_numbers_of_the_old_head_comment_are_not_read_for_the_new_head(
    world: _World, item: str, marker: str
) -> None:
    command = _one(item, marker)
    numbers = _numbers(command)
    world.comments(f"Lab: head={_OLD}\n{_naming(numbers)}\n")
    assert world.run(command) != 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "item",
    sorted(
        {
            "dod-omn25071-ac5",
            "dod-omn25071-ac6",
            "dod-omn25206-ac4",
            "dod-omn25281-ac6",
        }
    ),
)
def test_the_comment_checks_use_the_bus_read_and_never_the_option_the_tool_lacks(
    world: _World, item: str
) -> None:
    for check in _ITEMS[item]["checks"]:
        command = str(check["check_value"])
        assert "--comments" not in command
        if "pr-view" in command:
            assert "pr-view --source bus " in command
            assert "pr_comment_check.py" in command
    # The shim, like the tool, answers `--comments` with a usage error and a plain `pr-view` with no comments.
    assert world.run("onex scm pr-view --comments OmniNode-ai/omnimarket#1") == 2


@pytest.mark.unit
@pytest.mark.parametrize(("item", "marker"), _LAB_CHECKS + _FINDING_CHECKS)
def test_a_shape_the_real_tools_never_print_fails_the_comment_checks(
    world: _World, item: str, marker: str
) -> None:
    command = _one(item, marker)
    numbers = _numbers(command)
    good = _lab(_HEAD) + _naming(numbers)
    world.comments(good)
    assert world.run(command) == 0
    # Plain text where the tool prints JSON, and the watcher's answer (no comments key).
    world.comments(raw=good)
    assert world.run(command) != 0
    world.comments(raw=(world.root / "view.json").read_text(encoding="utf-8"))
    assert world.run(command) != 0
    # The PR watcher reader prints facts.head_sha; a flat head_sha is not what it prints.
    world.comments(good)
    world.state(shape="flat")
    assert world.run(command) != 0
    # A short head from the reader is refused, not matched as a prefix.
    world.state(head=_HEAD[:12])
    assert world.run(command) != 0


@pytest.mark.unit
def test_ac6_pr_checks_check_reads_the_required_check_runs_at_the_head(
    world: _World,
) -> None:
    command = _one("dod-omn25071-ac6", "pr-checks")
    assert "onex scm pr-checks --source local OmniNode-ai/omnimarket#6335" in command
    assert world.run(command) == 0
    world.checks(head=_OLD)
    assert world.run(command) != 0
    world.checks(red=("CI Summary",))
    assert world.run(command) != 0
    world.checks(pending=("lint",))
    assert world.run(command) != 0
    # The cancelled clause is read: dropping it would pass this reading.
    world.checks(cancelled=("lint",))
    assert world.run(command) != 0
    world.checks(runs=0)
    assert world.run(command) != 0
    # The flat shape and the bus answer are shapes the real tool prints for another source or never.
    world.checks(shape="flat")
    assert world.run(command) != 0


@pytest.mark.unit
def test_the_shims_print_the_keys_the_real_tools_printed() -> None:
    # Read on 2026-11-17 from the PR watcher reader (--json) and `onex scm pr-checks` for an open pull request.
    assert "facts" in _REAL_STATE_KEYS
    assert {"head_sha", "red", "pending", "cancelled", "runs"} <= (
        _REAL_WATCHER_CHECK_KEYS
    )
    comment_fields = set(ModelPrComment.model_fields)
    assert {"body", "created_at"} <= comment_fields
    assert "comments" in ModelPrTextResult.model_fields


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
        _commands("dod-omn25071-ac3", "cat-file")[0],
        _commands("dod-omn25071-ac3", "collect-only")[0],
    )
    ac4_exists = _commands("dod-omn25071-ac4", "cat-file")[0]
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
def test_the_contracts_name_the_plain_contract_path_and_no_link() -> None:
    for name in ("OMN-25071", "OMN-25206", "OMN-25281"):
        text = (_REPO / "contracts" / f"{name}.yaml").read_text(encoding="utf-8")
        assert "](https://" not in text
    text = (_REPO / "contracts" / "OMN-25071.yaml").read_text(encoding="utf-8")
    assert "contracts/OMN-24874.yaml" in text


_PYTEST_CHECK = re.compile(r"pytest\s+(?P<rest>[^|;&]*)")


def _selectors(command: str) -> list[tuple[list[str], str | None]]:
    """The test paths and the ``-k`` expression of each pytest invocation inside one check command."""
    found: list[tuple[list[str], str | None]] = []
    for match in _PYTEST_CHECK.finditer(command):
        try:
            tokens = shlex.split(match.group("rest"))
        except ValueError:
            continue
        paths = [t for t in tokens if t.startswith("tests") and not t.startswith("-")]
        expr = tokens[tokens.index("-k") + 1] if "-k" in tokens else None
        if paths:
            found.append((paths, expr))
    return found


@functools.cache
def _node_ids(path: str) -> tuple[str, ...]:
    """Every test id pytest collects under ``path`` (one collect-only run, with no selector)."""
    argv = [sys.executable, "-m", "pytest", path, "--collect-only", "-q"]
    argv += ["--no-cov", "-n", "0", "-p", "no:randomly"]
    done = subprocess.run(
        argv,
        cwd=_REPO,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    return tuple(line for line in done.stdout.splitlines() if "::" in line)


def _keyword_matcher(keywords: Sequence[str]) -> Callable[..., bool]:
    def has(token: str, /, **_: str | int | bool | None) -> bool:
        return any(token.lower() in k.lower() for k in keywords)

    return has


def _selected(paths: Sequence[str], expr: str | None) -> int:
    """How many collected tests ``-k expr`` selects under ``paths``: the path parts, the file name and the test name
    (with its parameters) are the keywords, each matched as a lowercase substring as pytest does."""
    matcher = Expression.compile(expr) if expr is not None else None
    count = 0
    for path in paths:
        for node_id in _node_ids(path):
            file_part, _, name = node_id.partition("::")
            keywords = [*file_part.split("/"), name]
            if matcher is None or matcher.evaluate(_keyword_matcher(keywords)):
                count += 1
    return count


@pytest.mark.unit
def test_every_hosted_dod_pytest_selector_selects_a_test_in_the_diff() -> None:
    # Review finding 3: a falsifier that selects no test cannot fail. Each dod check that the hosted runner runs
    # (no local_done_gate scope) names a file or selector that collects at least one test here.
    seen: dict[tuple[tuple[str, ...], str | None], int] = {}
    for contract in _CONTRACTS.values():
        for item in contract["dod_evidence"]:
            if item.get("execution_scope") == "local_done_gate":
                continue
            for check in item["checks"]:
                for paths, expr in _selectors(str(check["check_value"])):
                    seen.setdefault((tuple(paths), expr), 0)
    assert seen, "no hosted pytest check found"
    for found_paths, found_expr in seen:
        assert all((_REPO / p).exists() for p in found_paths), found_paths
        assert _selected(found_paths, found_expr) > 0, (found_paths, found_expr)


@pytest.mark.unit
def test_the_ticket_falsifier_selectors_select_a_test_in_the_alarm_node_tests() -> None:
    # OMN-25281 AC3 and AC4 name `-k "omn24874 and alarm"` and `-k "omn24874 and golden_chain"`.
    node_tests = "tests/nodes/node_delegation_scenario_alarm_effect"
    assert _selected([node_tests], "omn24874 and alarm") > 0
    assert _selected([node_tests], "omn24874 and golden_chain") > 0
    # The selector must be able to select nothing: a made-up token selects no test (the negative control).
    assert _selected([node_tests], "omn24874 and no_such_token_xyz") == 0
