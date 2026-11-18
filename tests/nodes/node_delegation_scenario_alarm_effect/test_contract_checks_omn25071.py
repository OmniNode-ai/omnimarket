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
import shutil
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
from omnimarket.nodes.node_github_repo_gateway_effect.dispatcher import dispatch
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    EnumGithubGatewayOperation,
    ModelGithubGatewayRequest,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    REPO as _GATEWAY_REPO,
)
from tests.nodes.node_github_repo_gateway_effect.recorded_transport import (
    JsonExchange,
    RecordedGatewayTransport,
    get,
    issue_comment,
    json_response,
    pull_request,
)

_REPO = Path(__file__).resolve().parents[3]
_CONTRACTS = {
    name: yaml.safe_load(
        (_REPO / "contracts" / f"{name}.yaml").read_text(encoding="utf-8")
    )
    for name in ("OMN-25071", "OMN-25206", "OMN-25281", "OMN-25319", "OMN-25376")
}
_HEAD = "0123456789abcdef0123456789abcdef01234567"
_OLD = "89abcdef0123456789abcdef0123456789abcdef"
_LAB = "onex lab-work run --bus-lane dev --lane x --kind test -- uv run pytest"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> Any:
    """Output the real tools printed, committed as it was read (see the fixtures directory)."""
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


_SOURCES = (
    "auto",
    "local",
    "bus",
)  # the choices `onex scm pr-view|pr-checks --source` declares


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
            'prev=""\nfor arg in "$@"; do\n'
            '  if [ "$prev" = "--source" ]; then\n'
            '    case "$arg" in auto|local|bus) ;; *) echo "Error: Invalid value for \'--source\'" >&2; exit 2 ;; esac\n'
            '  fi\n  prev="$arg"\ndone\n'
            'case "$2" in\n'
            f'  pr-view) case "$*" in *"--source bus"*) cat "{root}/comments.json" ;; *) cat "{root}/view.json" ;; esac ;;\n'
            f'  pr-checks) case "$*" in *"--source bus"*) cat "{root}/checks-bus.json" ;; *) cat "{root}/checks.json" ;; esac ;;\n'
            "  *) exit 2 ;;\nesac\n",
        )
        real_git = shutil.which("git")
        assert real_git is not None
        self._script(
            bin_dir / "git",
            f'#!/bin/sh\nif [ "$1" = rev-parse ] && [ "$2" = HEAD ]; then cat "{root}/git-head"; exit 0; fi\n'
            f'exec "{real_git}" "$@"\n',
        )
        self.git_head(_HEAD)
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
        # What `onex scm pr-view --source bus` printed when the bus was not reached: the watcher's answer, no comments.
        (root / "view.json").write_text(
            json.dumps(_fixture("pr_view_bus_read_without_bus_6353.json")),
            encoding="utf-8",
        )

    def git_head(self, sha: str) -> None:
        """What ``git rev-parse HEAD`` prints in the tree the Done gate runs in."""
        (self.root / "git-head").write_text(sha + "\n", encoding="utf-8")

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

    def state(
        self,
        *,
        head: str = _HEAD,
        shape: str = "real",
        fixture: str = "pr_state_local_open_6335.json",
    ) -> None:
        """The PR watcher reader's ``--json`` answer: the committed real output with ``facts.head_sha`` set."""
        document = _fixture(fixture)
        document["facts"]["head_sha"] = head
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
        answer = _fixture("pr_checks_local_6335.json")
        reading = answer["result"]
        row = next(
            r for r in reading["runs"] if r[1] == "completed" and r[2] == "success"
        )
        reading.update(
            head_sha=head,
            number=6335,
            red=list(red),
            pending=list(pending),
            cancelled=list(cancelled),
            red_facts=[],
            runs=[row] * runs,
        )
        reading["class"] = "green" if not (red or pending or cancelled) else "red"
        document = json.dumps(answer)
        if shape == "flat":
            document = json.dumps(reading)
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


def _round(command: str) -> str | None:
    found = re.search(r"--round (\d+)", command)
    return found.group(1) if found else None


def _naming(numbers: Sequence[int], rnd: str | None = None) -> str:
    tag = f"[round {rnd}] " if rnd else ""
    return "\n".join(f"{tag}finding {n}: argued with evidence" for n in numbers)


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
    rnd = _round(command)
    assert len(numbers) >= 4
    review = f"VERDICT CHANGES NEEDED head={_HEAD}\n{_naming(numbers, rnd)}\n"
    # AC1: a review that quotes every number, and no fixer comment for the head, passes nothing.
    world.comments(review)
    assert world.run(command) != 0
    world.comments(review, _lab(_OLD) + _naming(numbers, rnd))
    assert world.run(command) != 0
    # A fixer comment that names all but one number fails, even though the review names every number.
    for skipped in numbers:
        world.comments(
            review,
            f"Lab: head={_HEAD}\n"
            + _naming([n for n in numbers if n != skipped], rnd)
            + "\n",
        )
        assert world.run(command) != 0
    # A number that is a prefix of another does not read as that number.
    world.comments(f"Lab: head={_HEAD}\n" + _naming([n * 10 for n in numbers], rnd))
    assert world.run(command) != 0
    world.comments(review, f"Lab: head={_HEAD}\n{_naming(numbers, rnd)}\n")
    assert world.run(command) == 0


@pytest.mark.unit
@pytest.mark.parametrize(("item", "marker"), _FINDING_CHECKS)
def test_the_numbers_of_the_old_head_comment_are_not_read_for_the_new_head(
    world: _World, item: str, marker: str
) -> None:
    command = _one(item, marker)
    numbers = _numbers(command)
    world.comments(f"Lab: head={_OLD}\n{_naming(numbers, _round(command))}\n")
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
    good = _lab(_HEAD) + _naming(numbers, _round(command))
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
def test_the_contracts_name_the_plain_contract_path_and_no_link() -> None:
    for name in _CONTRACTS:
        text = (_REPO / "contracts" / f"{name}.yaml").read_text(encoding="utf-8")
        assert "](https://" not in text


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


# --- Review of head 37fe44a7a8ef (OMN-25319): findings 1 to 5 ------------------------------------------------------

_ALARM_TESTS = "tests/nodes/node_delegation_scenario_alarm_effect"
_GOLDEN_CHAIN = Path(__file__).resolve().parent / (
    "test_golden_chain_delegation_scenario_alarm.py"
)


_FALSIFIER = re.compile(r"falsifier: `(?P<command>[^`]+)`")


def _falsifier_commands(contract: dict[str, Any]) -> list[str]:
    """The command inside each ``falsifier: `...``` of the contract's acceptance statements."""
    return [
        match.group("command")
        for requirement in contract["requirements"]
        for criterion in requirement["acceptance"]
        for match in _FALSIFIER.finditer(str(criterion["statement"]))
    ]


def _selected_ids(paths: Sequence[str], expr: str | None) -> list[str]:
    """The test ids ``-k expr`` selects under ``paths`` (the keywords of ``_selected``)."""
    matcher = Expression.compile(expr) if expr is not None else None
    ids = []
    for path in paths:
        for node_id in _node_ids(path):
            file_part, _, name = node_id.partition("::")
            keywords = [*file_part.split("/"), name]
            if matcher is None or matcher.evaluate(_keyword_matcher(keywords)):
                ids.append(node_id)
    return ids


def _asserts_a_zero_tick_exit(function_name: str) -> bool:
    """True when the golden-chain test function holds ``assert main(...) == 0`` (the tick's exit code is read)."""
    import ast

    tree = ast.parse(_GOLDEN_CHAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Assert)
                    and isinstance(inner.test, ast.Compare)
                    and isinstance(inner.test.left, ast.Call)
                    and getattr(inner.test.left.func, "id", "") == "main"
                    and isinstance(inner.test.ops[0], ast.Eq)
                    and isinstance(inner.test.comparators[0], ast.Constant)
                    and inner.test.comparators[0].value == 0
                ):
                    return True
    return False


@pytest.mark.unit
@pytest.mark.parametrize("item", ["dod-omn25071-ac2", "dod-omn25206-ac2"])
def test_the_ac2_selector_selects_a_test_that_asserts_a_tick_exit_of_zero(
    item: str,
) -> None:
    # Review finding 1: `-k rendered` selected two tests and neither asserted a tick exit of 0.
    (check,) = _ITEMS[item]["checks"]
    ((paths, expr),) = _selectors(str(check["check_value"]))
    names = [node.partition("::")[2] for node in _selected_ids(paths, expr)]
    zero = [n for n in names if _asserts_a_zero_tick_exit(n.partition("[")[0])]
    assert zero, (expr, names)


@pytest.mark.unit
@pytest.mark.parametrize("contract", ["OMN-25071", "OMN-25206"])
def test_the_ac2_falsifier_text_selects_a_test_that_asserts_a_tick_exit_of_zero(
    contract: str,
) -> None:
    (command,) = [
        c
        for c in _falsifier_commands(_CONTRACTS[contract])
        if "golden_chain" in c
        and "-k" in c
        and "boundary" not in c
        and "bucketed_stale" not in c
    ]
    ((paths, expr),) = _selectors(command)
    names = [node.partition("::")[2] for node in _selected_ids(paths, expr)]
    assert any(_asserts_a_zero_tick_exit(n.partition("[")[0]) for n in names), names


@pytest.mark.unit
def test_the_ticket_falsifier_for_the_tick_exit_collects_a_test() -> None:
    # OMN-25319 AC1: `-k "omn24874 and tick_exit"` (the alarm node tests are the only ones under tests/nodes it can select) collects at least one test.
    assert _selected([_ALARM_TESTS], "omn24874 and tick_exit") > 0
    assert _selected([_ALARM_TESTS], "omn24874 and no_such_token_xyz") == 0


_NAMED_PATH = re.compile(
    r"(?<![\w/.-])(?:tests|contracts|src|scripts)/[\w./-]*[\w]\.(?:py|yaml|json)"
)


def _named_paths(contract: dict[str, Any]) -> set[str]:
    """Every repository file the contract's check commands and falsifier commands name (a test path, a script, a contract)."""
    commands = _falsifier_commands(contract) + [
        str(check["check_value"])
        for item in contract["dod_evidence"]
        for check in item["checks"]
    ]
    return {
        match.group(0)
        for command in commands
        for match in _NAMED_PATH.finditer(command)
    }


def _missing_paths(contract: dict[str, Any], root: Path) -> list[str]:
    """The files the contract names that do not exist under ``root`` (the head's tree)."""
    return sorted(path for path in _named_paths(contract) if not (root / path).exists())


@pytest.mark.unit
@pytest.mark.parametrize("name", list(_CONTRACTS))
def test_contract_checks_omn25071_paths_named_by_each_contract_exist_at_the_head(
    name: str,
) -> None:
    # Review of head 43bc9673d0c1, finding 2: the OMN-25071 contract named the cost pull request's files, absent at this
    # head, so `git cat-file -e HEAD:<path>` failed and the items were unmet. Every named path exists in this tree.
    assert _missing_paths(_CONTRACTS[name], _REPO) == []
    # The reading is real: the contract names paths, and each is also tracked by git at this head.
    named = _named_paths(_CONTRACTS[name])
    assert named
    listed = subprocess.run(
        ["git", "ls-files", "--", *sorted(named)],
        cwd=_REPO,
        env=scrub_git_location_env(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode == 0 and listed.stdout.strip():
        assert set(listed.stdout.split()) == named


@pytest.mark.unit
def test_contract_checks_omn25071_paths_check_fails_on_a_missing_falsifier_path() -> (
    None
):
    # The negative control, through the same reader: a contract whose falsifier names the cost pull request's test file
    # (absent here), and one whose check names a file that is not in the tree, are both reported.
    contract = {
        "requirements": [
            {
                "acceptance": [
                    {
                        "statement": "falsifier: `uv run --frozen pytest tests/test_omn24874_contract_checks.py -k ac3 -q` fails"
                    }
                ]
            }
        ],
        "dod_evidence": [
            {
                "checks": [
                    {
                        "check_value": "sh -c 'git cat-file -e HEAD:contracts/OMN-24874-absent-at-this-head.yaml'"
                    }
                ]
            }
        ],
    }
    assert _missing_paths(contract, _REPO) == [
        "contracts/OMN-24874-absent-at-this-head.yaml",
        "tests/test_omn24874_contract_checks.py",
    ]
    assert _missing_paths({"requirements": [], "dod_evidence": []}, _REPO) == []


@pytest.mark.unit
def test_contract_checks_omn25071_paths_every_selector_path_selects_at_this_head() -> (
    None
):
    seen = 0
    for name, contract in _CONTRACTS.items():
        commands = _falsifier_commands(contract) + [
            str(check["check_value"])
            for item in contract["dod_evidence"]
            for check in item["checks"]
        ]
        for command in commands:
            for paths, _ in _selectors(command):
                seen += 1
                assert all((_REPO / p).exists() for p in paths), (name, paths)
    assert seen > 10


@pytest.mark.unit
def test_the_old_omn25281_falsifier_path_is_not_a_file_in_the_tree() -> None:
    # The old OMN-25281 falsifier path is not a file in the tree (the negative control of the path reading).
    assert not (_REPO / "tests" / "test_contract_checks_omn25071.py").exists()
    assert (
        _REPO
        / "tests"
        / "nodes"
        / "node_delegation_scenario_alarm_effect"
        / "test_contract_checks_omn25071.py"
    ).exists()
    assert "tests/test_contract_checks_omn25071.py" not in "".join(
        _falsifier_commands(_CONTRACTS["OMN-25281"])
    )


def _real_checks_result() -> dict[str, Any]:
    return dict(_fixture("pr_checks_local_6335.json")["result"])


@pytest.mark.unit
def test_the_shim_reading_has_exactly_the_keys_of_the_real_pr_checks_output(
    world: _World,
) -> None:
    # Review finding 3: the keys were hand-copied. The committed output of `onex scm pr-checks --source local` for
    # omnimarket#6335 is the reference; the shim's reading is compared to it key for key.
    real = _fixture("pr_checks_local_6335.json")
    assert real["source"] == "watcher"
    shim = json.loads((world.root / "checks.json").read_text(encoding="utf-8"))
    assert set(shim) == set(real)
    assert set(shim["result"]) == set(real["result"])
    assert set(shim["result"]) == set(_real_checks_result())


@pytest.mark.unit
@pytest.mark.parametrize(
    "fixture", ["pr_state_local_open_6335.json", "pr_state_local_merged_6351.json"]
)
def test_the_shim_state_has_the_keys_of_the_real_state_reader_for_open_and_merged(
    world: _World, fixture: str
) -> None:
    real = _fixture(fixture)
    script = world.state_script.read_text(encoding="utf-8")
    shim = json.loads(
        subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert set(shim) == set(real)
    assert set(shim["facts"]) == set(real["facts"])
    assert len(real["facts"]["head_sha"]) == 40


@pytest.mark.unit
def test_a_merged_pull_request_still_prints_facts_head_sha() -> None:
    merged = _fixture("pr_state_local_merged_6351.json")
    assert merged["facts"]["state"] == "MERGED"
    assert merged["facts"]["merged_at"]
    assert FULL_SHA.match(merged["facts"]["head_sha"])


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


@pytest.mark.unit
@pytest.mark.parametrize("verb", ["pr-view", "pr-checks"])
@pytest.mark.parametrize("source", ["watcher", "nonsense", ""])
def test_the_shim_refuses_a_source_the_tool_does_not_accept(
    world: _World, verb: str, source: str
) -> None:
    # `onex scm <verb> --source` takes auto, local or bus (`--help` of both verbs); the answer's own `source` is
    # `watcher` or `bus`, which is not an input value.
    assert (
        world.run(f"onex scm {verb} --source '{source}' OmniNode-ai/omnimarket#1") == 2
    )
    for accepted in ("auto", "local", "bus"):
        assert (
            world.run(f"onex scm {verb} --source {accepted} OmniNode-ai/omnimarket#1")
            == 0
        )


@pytest.mark.unit
def test_the_c1_checks_pass_a_source_the_tool_accepts() -> None:
    for item in ("dod-omn25071-ac5", "dod-omn25071-ac6"):
        for check in _ITEMS[item]["checks"]:
            for source in re.findall(r"--source (\S+)", str(check["check_value"])):
                assert source in {"auto", "local", "bus"}, source


_REVIEW_FINDING_5 = [
    ("a_line_with_exit_one_and_expected_exit_zero", "exit 1, expected exit 0"),
    ("not_exit_zero", "result: exit 1 (not exit 0)"),
]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "text"), _REVIEW_FINDING_5, ids=[n for n, _ in _REVIEW_FINDING_5]
)
@pytest.mark.parametrize(("item", "marker"), _LAB_CHECKS)
def test_lab_comment_check_reads_the_exit_of_a_line_not_an_exit_zero_inside_a_failure(
    world: _World, item: str, marker: str, name: str, text: str
) -> None:
    del name
    world.comments(f"Lab: head={_HEAD}\nhost h053, command: {_LAB}\n{text}\n")
    assert world.run(_one(item, marker)) != 0


@pytest.mark.unit
@pytest.mark.parametrize(("item", "marker"), _LAB_CHECKS)
def test_lab_comment_check_does_not_read_collab_runs_as_a_lab_command(
    world: _World, item: str, marker: str
) -> None:
    world.comments(f"Lab: head={_HEAD}\nthe collab runs were slow\nresult: exit 0\n")
    assert world.run(_one(item, marker)) != 0


@pytest.mark.unit
@pytest.mark.parametrize(("item", "marker"), _LAB_CHECKS)
def test_lab_comment_check_reads_a_red_run_on_another_line_beside_the_exit_zero_line(
    world: _World, item: str, marker: str
) -> None:
    world.comments(
        f"Lab: head={_HEAD}\nred at the old head: exit 1\n"
        f"green: command: {_LAB}\nresult: exit 0\n"
    )
    assert world.run(_one(item, marker)) == 0


@pytest.mark.unit
def test_pr_comment_check_refuses_a_truncated_comment_or_comment_list(
    world: _World,
) -> None:
    command = _one("dod-omn25071-ac6", "--lab")
    world.comments(_lab(_HEAD))
    assert world.run(command) == 0
    document = json.loads((world.root / "comments.json").read_text(encoding="utf-8"))
    document["result"]["comments"][0]["body_truncated"] = True
    world.comments(raw=json.dumps(document))
    assert world.run(command) != 0
    document["result"]["comments"][0]["body_truncated"] = False
    document["result"]["truncated"] = True
    world.comments(raw=json.dumps(document))
    assert world.run(command) != 0


@pytest.mark.unit
def test_pr_comment_check_reads_the_author_of_the_comment(world: _World) -> None:
    command = _one("dod-omn25071-ac6", "--lab")
    world.comments(_lab(_HEAD))
    document = json.loads((world.root / "comments.json").read_text(encoding="utf-8"))
    document["result"]["comments"][0]["author"] = ""
    world.comments(raw=json.dumps(document))
    assert world.run(command) != 0
    document["result"]["comments"][0]["author"] = "lane"
    world.comments(raw=json.dumps(document))
    assert world.run(command.replace("--lab'", "--lab --author lane'")) == 0
    assert world.run(command.replace("--lab'", "--lab --author other'")) != 0


@pytest.mark.unit
def test_one_finding_eight_does_not_satisfy_two_reviews(world: _World) -> None:
    # Review finding 5: the OMN-25206 checks (review 0525) and the OMN-25281 checks (review 0655) both read a bare
    # `finding 8`, which names different findings of different reviews.
    c2 = _one("dod-omn25206-ac4", "--finding")
    c3 = _one("dod-omn25281-ac6", "--finding")
    assert "--round 0525" in c2
    assert "--round 0655" in c3
    numbers2, numbers3 = _numbers(c2), _numbers(c3)
    bare = f"Lab: head={_HEAD}\n" + _naming(sorted({*numbers2, *numbers3}))
    world.comments(bare)
    assert world.run(c2) != 0
    assert world.run(c3) != 0
    both = (
        f"Lab: head={_HEAD}\n"
        + "\n".join(f"[round 0525] finding {n}: still fixed" for n in numbers2)
        + "\n"
        + "\n".join(f"[round 0655] finding {n}: see below" for n in numbers3)
        + "\n"
    )
    world.comments(both)
    assert world.run(c2) == 0
    assert world.run(c3) == 0
    only_0655 = f"Lab: head={_HEAD}\n" + "\n".join(
        f"[round 0655] finding {n}: x" for n in sorted({*numbers2, *numbers3})
    )
    world.comments(only_0655)
    assert world.run(c2) != 0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("item", "contains"),
    [("dod-omn25206-ac4", "--finding"), ("dod-omn25281-ac6", "--finding")],
)
def test_the_watcher_head_of_6353_is_compared_with_the_head_of_the_tree(
    world: _World, item: str, contains: str
) -> None:
    command = _one(item, contains)
    assert "git rev-parse HEAD" in command
    numbers = _numbers(command)
    rounds = re.search(r"--round (\d+)", command)
    assert rounds is not None
    good = f"Lab: head={_HEAD}\n" + "\n".join(
        f"[round {rounds.group(1)}] finding {n}: x" for n in numbers
    )
    world.comments(good)
    world.git_head(_HEAD)
    assert world.run(command) == 0
    world.git_head(_OLD)
    assert world.run(command) != 0


# --- The bus read with comments, as the real reader prints it (review of head 43bc9673d0c1, finding 4) ---------------

_REAL_BUS_FIXTURE = "pr_view_bus_with_comments_6353.json"
_REAL_BUS_HEAD = "43bc9673d0c1db0f70d8497d12983757ed85649d"


def _real_reader_bus_answer() -> dict[str, Any]:
    """What ``onex scm pr-view --source bus`` prints: the gateway's own ``read_pr_text`` over recorded GitHub answers.

    No lab host reaches the bus from a lane (``--source bus`` there answers from the watcher with no comments), so the
    reader that builds ``result`` (``read_pr_text`` through the gateway's ``dispatch``) runs over recorded GitHub REST
    answers, and its typed result is wrapped by the real ``ModelScmAnswer`` and ``ModelScmBusReceipt``.
    """
    base = f"/repos/{_GATEWAY_REPO}"
    pr = pull_request(6353, body="")
    pr["head"] = {
        "ref": "alarm",
        "sha": _REAL_BUS_HEAD,
        "repo": {"full_name": _GATEWAY_REPO},
    }
    pr["draft"] = True
    pr["comments"] = 2
    older = issue_comment(
        101, body=f"Lab: head={_OLD}\nhost h053, command: {_LAB}\nresult: exit 0\n"
    )
    newer = issue_comment(
        102,
        body=f"Lab: head={_REAL_BUS_HEAD}\nhost h053, command: {_LAB}\nresult: exit 0\n"
        "[round 1345] finding 1: fixed\n",
    )
    older["user"] = newer["user"] = {"login": "lane-writer", "id": 9}
    older["created_at"] = "2026-11-17T13:00:00Z"
    newer["created_at"] = "2026-11-17T15:00:00Z"
    request = ModelGithubGatewayRequest.model_validate(
        {
            "operation": EnumGithubGatewayOperation.PR_TEXT,
            "repo": _GATEWAY_REPO,
            "pr_number": 6353,
            "limit": 100,
        }
    )
    transport = RecordedGatewayTransport(
        [
            JsonExchange(get(f"{base}/pulls/6353"), json_response(pr)),
            JsonExchange(
                get(f"{base}/issues/6353/comments?per_page=100"),
                json_response([older, newer]),
            ),
        ]
    )
    result = dispatch(request, transport)
    transport.assert_drained()
    answer = ModelScmAnswer(
        verb="pr-view",
        node=EnumScmNode.GATEWAY,
        operation="pr_text",
        repo=_GATEWAY_REPO,
        source="bus",
        outcome="completed",
        watcher_tick=None,
        bus=ModelScmBusReceipt(
            correlation_id=UUID(int=1),
            command_topic="onex.cmd.x.v1",
            command_envelope_id=UUID(int=2),
            terminal_topic="onex.evt.x.v1",
        ),
        result=result.model_dump(mode="json"),
    )
    return dict(json.loads(answer.model_dump_json()))


def _key_shape(value: Any) -> Any:
    """The keys of a JSON document at every depth (a list reads as the shape of its first item)."""
    if isinstance(value, dict):
        return {key: _key_shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_key_shape(value[0])] if value else []
    return None


@pytest.mark.unit
def test_pr_comment_check_real_fixture_is_what_the_real_reader_prints() -> None:
    # The committed answer is regenerated by the real reader: a hand edit, or a reader change, fails here.
    assert _fixture(_REAL_BUS_FIXTURE) == _real_reader_bus_answer()


@pytest.mark.unit
def test_pr_comment_check_real_fixture_has_the_shape_the_shim_prints(
    world: _World,
) -> None:
    # The shim of every comment check prints the keys of the real reader's answer, to the last nested key.
    world.comments(f"Lab: head={_REAL_BUS_HEAD}\nx", head=_REAL_BUS_HEAD)
    shim = json.loads((world.root / "comments.json").read_text(encoding="utf-8"))
    real = _fixture(_REAL_BUS_FIXTURE)
    assert _key_shape(shim) == _key_shape(real)
    assert real["source"] == "bus"
    assert real["bus"] is not None


@pytest.mark.unit
def test_pr_comment_check_real_fixture_is_read_by_the_checker(world: _World) -> None:
    # The checker reads the real answer itself, not only the shim's: the newest Lab comment for the head is the one read.
    (world.root / "comments.json").write_text(
        json.dumps(_fixture(_REAL_BUS_FIXTURE)), encoding="utf-8"
    )
    world.git_head(_REAL_BUS_HEAD)
    pipe = (
        f"onex scm pr-view --source bus OmniNode-ai/omnimarket#6353 | {sys.executable} "
        f"{_REPO}/tests/nodes/node_delegation_scenario_alarm_effect/pr_comment_check.py"
    )
    assert (
        world.run(f"{pipe} --head {_REAL_BUS_HEAD} --lab --finding 1 --round 1345") == 0
    )
    assert (
        world.run(f"{pipe} --head {_REAL_BUS_HEAD} --lab --finding 2 --round 1345") != 0
    )
    assert world.run(f"{pipe} --head {_HEAD} --lab") != 0
