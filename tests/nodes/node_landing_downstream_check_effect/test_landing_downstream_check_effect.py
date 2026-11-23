# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing downstream check holds a head that breaks a downstream repository (OMN-26513).

The replay shape is omnibase_core#2361: a core release dropped a name three omnimarket files imported, every
omnimarket pull request then read red, and nothing checked the core head against omnimarket before it merged. The
scripted runner models the venv and install steps; the smoke step runs the real ``tests/ci/latest_core_smoke.py``
against a fake core that lacks (or keeps) the name, so the parser is tied to the smoke's real output.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_downstream_gate import (
    EnumDownstreamVerdict,
    ModelDownstreamGateFact,
)
from omnimarket.nodes.node_landing_downstream_check_effect.handlers.handler_landing_downstream_check import (
    HandlerLandingDownstreamCheck,
)
from omnimarket.nodes.node_landing_downstream_check_effect.models import (
    ModelCommandOutcome,
    ModelDownstreamTarget,
    ModelLandingDownstreamCheckRequest,
)
from omnimarket.nodes.node_landing_downstream_check_effect.runtime_landing_downstream_check_cli import (
    main as cli_main,
)

REPO = Path(__file__).resolve().parents[3]
SMOKE = REPO / "tests" / "ci" / "latest_core_smoke.py"
FIXTURES = REPO / "tests" / "ci" / "fixtures" / "latest_core_smoke"
NODE = REPO / "src" / "omnimarket" / "nodes" / "node_landing_downstream_check_effect"
HEAD = "12e5044971c3cf2dd1160b14c35bd0d63c920578"
PACKAGE = "node_run_node_user"


def _fake_core(root: Path, *, symbols: tuple[str, ...]) -> Path:
    cli = root / "omnibase_core" / "cli"
    cli.mkdir(parents=True)
    (root / "omnibase_core" / "__init__.py").write_text("")
    (cli / "__init__.py").write_text("")
    body = "".join(f"def {n}(*args, **kwargs):\n    return None\n\n" for n in symbols)
    (cli / "cli_run_node.py").write_text(textwrap.dedent(body) or "# empty\n")
    return root


def _request(
    tmp_path: Path, *names: str, test_args: tuple[str, ...] = ()
) -> ModelLandingDownstreamCheckRequest:
    return ModelLandingDownstreamCheckRequest(
        repo="omnibase_core",
        package="omnibase-core",
        repo_url="https://github.com/OmniNode-ai/omnibase_core.git",
        head_sha=HEAD,
        targets=tuple(
            ModelDownstreamTarget(name=n, checkout=tmp_path / n, test_args=test_args)
            for n in (names or ("omnimarket", "omnibase_infra"))
        ),
        smoke_script=SMOKE,
        work_dir=tmp_path / "work",
    )


class ScriptedRunner:
    """Answers each step by name; ``smoke`` maps a downstream name to its smoke outcome, others succeed."""

    def __init__(
        self,
        smoke: dict[str, ModelCommandOutcome] | None = None,
        *,
        fail_step: str = "",
        hang_step: str = "",
        extra: ModelCommandOutcome | None = None,
        installed_commit: str = HEAD,
    ) -> None:
        self.installed_commit = installed_commit
        self.smoke = smoke or {}
        self.fail_step = fail_step
        self.hang_step = hang_step
        self.extra = extra
        self.calls: list[tuple[list[str], Path | None]] = []

    @staticmethod
    def _step(argv: Sequence[str]) -> str:
        if argv[1] == "venv":
            return "venv"
        if argv[1] == "-I":
            return "probe-head"
        if argv[1] == "pip":
            return "install-head" if "--no-deps" in argv else "install-downstream"
        return "smoke" if argv[1].endswith("latest_core_smoke.py") else "tests"

    def __call__(
        self, argv: Sequence[str], timeout_seconds: float, cwd: Path | None
    ) -> ModelCommandOutcome:
        self.calls.append((list(argv), cwd))
        step = self._step(argv)
        if step == self.hang_step:
            return ModelCommandOutcome(returncode=-1, timed_out=True)
        if step == self.fail_step:
            return ModelCommandOutcome(returncode=1, stderr="the step failed")
        if step == "smoke":
            assert cwd is not None
            return self.smoke.get(cwd.name, _OK_SMOKE)
        if step == "probe-head":
            return ModelCommandOutcome(
                returncode=0, stdout=self.installed_commit + "\n"
            )
        if step == "tests":
            return self.extra or ModelCommandOutcome(returncode=0)
        return ModelCommandOutcome(returncode=0)


_OK_SMOKE = ModelCommandOutcome(
    returncode=0,
    stdout="LATEST-CORE-SMOKE packages=3 modules_tried=40 failed=0\n",
)


def _broken(at: str, symbol: str = "omnibase_core.models.ModelLedgerRender") -> str:
    return (
        f"LATEST-CORE-SMOKE FAIL node=node_create_ticket module=omnimarket.x symbol={symbol} "
        f"at={at} error=ImportError: cannot import name 'ModelLedgerRender'\n"
        "LATEST-CORE-SMOKE packages=3 modules_tried=40 failed=1\n"
    )


def test_every_downstream_passing_is_a_pass(tmp_path: Path) -> None:
    runner = ScriptedRunner()
    fact = HandlerLandingDownstreamCheck(runner).handle(_request(tmp_path))
    assert fact.verdict is EnumDownstreamVerdict.PASS
    assert [t.downstream for t in fact.targets] == ["omnimarket", "omnibase_infra"]
    assert fact.reasons == ()


def test_head_is_installed_from_the_pull_request_sha_into_each_venv(
    tmp_path: Path,
) -> None:
    runner = ScriptedRunner()
    HandlerLandingDownstreamCheck(runner).handle(_request(tmp_path, "omnimarket"))
    installs = [a for a, _ in runner.calls if "--no-deps" in a]
    assert len(installs) == 1
    assert (
        f"omnibase-core @ git+https://github.com/OmniNode-ai/omnibase_core.git@{HEAD}"
        in installs[0]
    )
    smoke_argv, smoke_cwd = runner.calls[-1]
    assert smoke_argv[1] == str(SMOKE)
    assert smoke_cwd == tmp_path / "omnimarket"


def test_a_break_names_the_importing_file_and_line_and_holds_the_head(
    tmp_path: Path,
) -> None:
    broken = ModelCommandOutcome(
        returncode=1,
        stdout=_broken(
            "src/omnimarket/nodes/node_create_ticket/handlers/core_ticket_guard.py:20"
        ),
    )
    runner = ScriptedRunner({"omnimarket": broken})
    fact = HandlerLandingDownstreamCheck(runner).handle(_request(tmp_path))
    assert fact.verdict is EnumDownstreamVerdict.BREAK
    assert fact.targets[0].verdict is EnumDownstreamVerdict.BREAK
    assert fact.targets[1].verdict is EnumDownstreamVerdict.PASS
    assert fact.targets[0].breaks[0].at.endswith("core_ticket_guard.py:20")
    assert any("core_ticket_guard.py:20" in r for r in fact.reasons)


def test_a_break_in_the_second_repository_alone_holds_the_head(
    tmp_path: Path,
) -> None:
    broken = ModelCommandOutcome(returncode=1, stdout=_broken("src/x.py:3"))
    fact = HandlerLandingDownstreamCheck(
        ScriptedRunner({"omnibase_infra": broken})
    ).handle(_request(tmp_path))
    assert fact.verdict is EnumDownstreamVerdict.BREAK
    assert fact.targets[0].verdict is EnumDownstreamVerdict.PASS


def test_an_install_that_left_a_release_in_place_is_unread_never_a_pass(
    tmp_path: Path,
) -> None:
    """Replaying omnibase_core#2361: the install exited zero ("Checked 1 package") and the head never replaced 0.47.77."""
    for left in ("", "a011726b9141bea6555b7288621838a06c0dfc18"):
        fact = HandlerLandingDownstreamCheck(
            ScriptedRunner(installed_commit=left)
        ).handle(_request(tmp_path, "omnimarket"))
        assert fact.verdict is EnumDownstreamVerdict.UNREAD
        assert "head-not-installed" in fact.reasons[0]


def test_the_head_is_installed_with_no_config_from_the_work_directory(
    tmp_path: Path,
) -> None:
    """The downstream checkout's own installer config must not constrain the package under test."""
    runner = ScriptedRunner()
    HandlerLandingDownstreamCheck(runner).handle(_request(tmp_path, "omnimarket"))
    argv, cwd = next((a, c) for a, c in runner.calls if "--no-deps" in a)
    assert "--no-config" in argv
    assert cwd == tmp_path / "work"


@pytest.mark.parametrize("step", ["venv", "install-downstream", "install-head"])
def test_a_step_that_fails_is_unread_never_a_pass(tmp_path: Path, step: str) -> None:
    fact = HandlerLandingDownstreamCheck(ScriptedRunner(fail_step=step)).handle(
        _request(tmp_path, "omnimarket")
    )
    assert fact.verdict is EnumDownstreamVerdict.UNREAD
    assert f"{step}-failed:rc=1" in fact.reasons[0]


def test_a_smoke_that_times_out_is_unread(tmp_path: Path) -> None:
    fact = HandlerLandingDownstreamCheck(ScriptedRunner(hang_step="smoke")).handle(
        _request(tmp_path, "omnimarket")
    )
    assert fact.verdict is EnumDownstreamVerdict.UNREAD
    assert fact.reasons == ("omnimarket: timeout:smoke",)


@pytest.mark.parametrize(
    "outcome",
    [
        ModelCommandOutcome(
            returncode=2,
            stdout="LATEST-CORE-SMOKE FAIL no packages found to import\n",
        ),
        ModelCommandOutcome(returncode=1, stdout="Traceback: the smoke crashed\n"),
        ModelCommandOutcome(
            returncode=0,
            stdout="LATEST-CORE-SMOKE packages=0 modules_tried=0 failed=0\n",
        ),
    ],
)
def test_a_smoke_that_imported_nothing_or_named_no_failure_is_not_a_pass(
    tmp_path: Path, outcome: ModelCommandOutcome
) -> None:
    fact = HandlerLandingDownstreamCheck(
        ScriptedRunner({"omnimarket": outcome})
    ).handle(_request(tmp_path, "omnimarket"))
    assert fact.verdict is EnumDownstreamVerdict.UNREAD


def test_a_failing_extra_command_is_a_break_naming_its_file_and_line(
    tmp_path: Path,
) -> None:
    extra = ModelCommandOutcome(
        returncode=2,
        stdout="ERROR tests/nodes/node_create_ticket/test_core_ticket_guard.py:11 ImportError\n",
    )
    fact = HandlerLandingDownstreamCheck(ScriptedRunner(extra=extra)).handle(
        _request(
            tmp_path, "omnimarket", test_args=("-m", "pytest", "--collect-only", "-q")
        )
    )
    assert fact.verdict is EnumDownstreamVerdict.BREAK
    assert (
        fact.targets[0].breaks[0].at
        == "tests/nodes/node_create_ticket/test_core_ticket_guard.py:11"
    )


def _replay_runner(core: Path) -> ScriptedRunner:
    """Venv and install steps scripted; the smoke step is the real script run against ``core`` and the fixture."""
    base = ScriptedRunner()

    def run(
        argv: Sequence[str], timeout_seconds: float, cwd: Path | None
    ) -> ModelCommandOutcome:
        if base._step(argv) != "smoke":
            return base(argv, timeout_seconds, cwd)
        done = subprocess.run(
            [sys.executable, argv[1], "--module", PACKAGE],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            cwd=FIXTURES,
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.pathsep.join([str(core), str(FIXTURES)]),
            },
        )
        return ModelCommandOutcome(
            returncode=done.returncode, stdout=done.stdout, stderr=done.stderr
        )

    return run  # type: ignore[return-value]


def test_replay_core_head_that_dropped_a_name_is_held_with_the_import_line(
    tmp_path: Path,
) -> None:
    """The 2361 shape: the head lacks a name a downstream file imports, so the real smoke names that file and line."""
    core = _fake_core(tmp_path / "core", symbols=("some_other_helper",))
    fact = HandlerLandingDownstreamCheck(_replay_runner(core)).handle(
        _request(tmp_path, "omnimarket")
    )
    assert fact.verdict is EnumDownstreamVerdict.BREAK, fact.reasons
    only = fact.targets[0].breaks[0]
    assert only.symbol == "omnibase_core.cli.cli_run_node.run_node_in_process"
    assert only.at.endswith("node_run_node_user/cli.py:5")


def test_replay_control_core_head_that_keeps_the_name_passes(tmp_path: Path) -> None:
    core = _fake_core(tmp_path / "core", symbols=("run_node_in_process",))
    fact = HandlerLandingDownstreamCheck(_replay_runner(core)).handle(
        _request(tmp_path, "omnimarket")
    )
    assert fact.verdict is EnumDownstreamVerdict.PASS, fact.reasons


def test_fact_line_counts_targets_and_breaks(tmp_path: Path) -> None:
    broken = ModelCommandOutcome(returncode=1, stdout=_broken("src/x.py:3"))
    fact = HandlerLandingDownstreamCheck(ScriptedRunner({"omnimarket": broken})).handle(
        _request(tmp_path)
    )
    assert fact.line() == (
        f"downstream_check=break repo=omnibase_core head={HEAD[:12]} targets=2 breaks=1"
    )
    assert ModelDownstreamGateFact.model_validate_json(fact.model_dump_json()) == fact


def test_cli_answers_2_on_a_request_that_does_not_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert cli_main([]) == 2


def test_contract_names_the_handler_and_the_entry_point_is_registered() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["handler"]["class"] == "HandlerLandingDownstreamCheck"
    assert contract["output_model"] == (
        "omnimarket.models.model_downstream_gate.ModelDownstreamGateFact"
    )
    assert (
        "node_landing_downstream_check_effect ="
        in (REPO / "pyproject.toml").read_text()
    )
