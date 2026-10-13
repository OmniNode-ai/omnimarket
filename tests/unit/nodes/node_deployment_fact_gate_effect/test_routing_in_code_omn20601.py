# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate refuses a new code path that chooses a route (OMN-20601).

Ruling 2026-10-10T17:54:30Z: routing decisions come from overlays, never from
omnimarket code. The deployment-fact gate node compares the route-selecting
comparisons in the package's non-test code with those at a base revision, and
refuses one the base did not carry. These cases plant a branch in a throwaway git
repository and run the gate's own entry point against it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_deployment_fact_gate_effect.__main__ import main
from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    PACKAGED_CONFIG_DIR,
    PACKAGED_ROUTING_CONFIGS,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_routing_in_code import (
    RoutingInCodeGateError,
    judge_routing_branches,
    judge_tree,
    read_base_sources,
    read_head_sources,
    routing_branches,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HANDLER = "src/omnimarket/nodes/node_planted/handlers/handler_planted.py"
_NEUTRAL = textwrap.dedent(
    """\
    def pick(request, mode):
        if mode == "fast":
            return "neutral"
        return "default"
    """
)
_BRANCH = textwrap.dedent(
    """\
    def pick(request, mode):
        if mode == "fast":
            return "neutral"
        if request.task_class == "summarization":
            return "cheap"
        return "default"
    """
)


@pytest.fixture(autouse=True)
def _scrub_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook or CI runner may export GIT_DIR or GIT_INDEX_FILE; the planted repo must not see them."""
    for name in [n for n in os.environ if n.startswith("GIT_")]:
        monkeypatch.delenv(name)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=probe",
            "-c",
            "user.email=probe@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )


@pytest.fixture
def planted_repo(tmp_path: Path) -> Path:
    """A repository whose HEAD carries the three packaged configs and a neutral handler."""
    repo = tmp_path / "repo"
    configs = repo / PACKAGED_CONFIG_DIR
    configs.mkdir(parents=True)
    for name in PACKAGED_ROUTING_CONFIGS:
        shutil.copy(_REPO_ROOT / PACKAGED_CONFIG_DIR / name, configs / name)
    handler = repo / _HANDLER
    handler.parent.mkdir(parents=True)
    handler.write_text(_NEUTRAL, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_routing_in_code_refused_when_the_tree_adds_a_branch(
    planted_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--repo-root", str(planted_repo), "--base", "HEAD"]) == 0

    (planted_repo / _HANDLER).write_text(_BRANCH, encoding="utf-8")
    assert main(["--repo-root", str(planted_repo), "--base", "HEAD"]) == 1

    refused = capsys.readouterr().err
    assert (
        f"{_HANDLER}::pick::request.task_class == 'summarization' (0 -> 1)" in refused
    )
    assert "read the choice from the deployment overlay" in refused
    new, inventory = judge_tree(planted_repo, "HEAD")
    assert [branch.fingerprint for branch in new] == [
        f"{_HANDLER}::pick::request.task_class == 'summarization'"
    ]
    assert inventory == (f"{_HANDLER}::pick::request.task_class == 'summarization'",)


def test_routing_in_code_refused_counts_a_second_copy_but_not_a_move_out_of_code(
    planted_repo: Path,
) -> None:
    (planted_repo / _HANDLER).write_text(_BRANCH, encoding="utf-8")
    _git(planted_repo, "commit", "-q", "-am", "branch already in the base")
    assert judge_tree(planted_repo, "HEAD")[0] == ()

    (planted_repo / _HANDLER).write_text(_NEUTRAL, encoding="utf-8")
    assert judge_tree(planted_repo, "HEAD")[0] == ()

    twice = _BRANCH.replace(
        'return "cheap"\n',
        'return "cheap"\n    if request.task_class == "summarization":\n        return "again"\n',
    )
    (planted_repo / _HANDLER).write_text(twice, encoding="utf-8")
    new, _ = judge_tree(planted_repo, "HEAD")
    assert [(b.base_count, b.head_count) for b in new] == [(1, 2)]


def test_routing_in_code_refused_cannot_judge_an_unresolvable_base(
    planted_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--repo-root", str(planted_repo), "--base", "no-such-revision"]) == 2
    assert "cannot judge" in capsys.readouterr().err
    with pytest.raises(RoutingInCodeGateError):
        read_base_sources(planted_repo, "no-such-revision")


def test_routing_in_code_refused_sees_each_selector_form_and_no_neutral_one() -> None:
    source = textwrap.dedent(
        """\
        def pick(request, mode):
            if request.task_class == "summarization":
                return 1
            if "docs" == request.tier:
                return 2
            if request.provider in ("a", "b"):
                return 3
            if request.backend_id != "local-coder":
                return 4
            if mode == "fast":
                return 5
            if "x" in request.model_name:
                return 6
            if request.task_class == other:
                return 7
            return 8
        """
    )
    path = "src/omnimarket/nodes/node_planted/handlers/handler_planted.py"
    found = routing_branches([(path, source)])
    assert sorted(found) == sorted(
        f"{path}::pick::{expression}"
        for expression in (
            "request.task_class == 'summarization'",
            "'docs' == request.tier",
            "request.provider in ('a', 'b')",
            "request.backend_id != 'local-coder'",
        )
    )


def test_routing_in_code_refused_ignores_test_code() -> None:
    source = "def f(r):\n    return r.task_class == 'summarization'\n"
    assert not routing_branches(
        [
            ("src/omnimarket/nodes/node_x/tests/test_x.py", source),
            ("src/omnimarket/nodes/node_x/handlers/test_y.py", source),
            ("src/omnimarket/nodes/node_x/conftest.py", source),
        ]
    )
    assert routing_branches([("src/omnimarket/nodes/node_x/handlers/h.py", source)])


def test_routing_in_code_refused_scanner_is_not_blind_on_the_real_tree() -> None:
    """Positive control: the tree carries route-selecting code today, and judged against itself it is not new."""
    head = routing_branches(read_head_sources(_REPO_ROOT))
    assert head, "the scan found no route-selecting comparison in src/omnimarket"
    assert judge_routing_branches(head, head) == ()
    assert len(judge_routing_branches(head, type(head)())) == len(head)
