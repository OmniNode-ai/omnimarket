# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Running the rows loader the way the host's overlays say, and what the reader may then do (OMN-24059).

Golden chain: the loader's rows come back untouched (exit 0), or the loader read the file itself (exit 10).
Error chain: a loader that times out, cannot run or fails is exit 11 when the loader overlay allows the file
fallback and exit 69 naming the reason when it forbids it; a loader that reports the ledger unreadable and a
missing or malformed overlay are exit 69 whatever the flag says. The exit codes are the seam the plugin's
drain_map reads (an exact table). Named break: a handler that answers fallback-allowed while the overlay forbids
the file fails the flag check.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    ModelRowsLoaderOverlay,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.adapters import (
    InMemoryLoaderSettingsSink,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.handlers import (
    HandlerLoaderRun,
    HandlerLoaderSettings,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    EnumLoaderRunStatus,
    ModelLoaderHostsOverlay,
    ModelLoaderRunRequest,
    ModelLoaderRunResult,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.runtime_rows_loader_settings import (
    EXIT_RUN_ANSWERED,
    EXIT_RUN_FALLBACK_ALLOWED,
    EXIT_RUN_FILE_FALLBACK_ANSWERED,
    EXIT_RUN_UNREADABLE,
    main,
)
from tests.unit.nodes.node_ledger_rows_loader_settings_effect.conftest_support import (
    HEAD,
    host_row,
    write_overlays,
)

pytestmark = pytest.mark.unit

ROWS = "2026-11-12T10:00:00Z | STATUS | lane=a | row\n"
ARGS = ("--reader", "drain-map", "--window-hours", "24")
HOSTS = ModelLoaderHostsOverlay.model_validate(
    {
        "schema_version": "ledger_rows_loader_hosts.v1",
        "timeout_seconds": 45,
        "hosts": [{"host": "hp-gen8", "command": ["loader", "-m", "x"]}],
    }
)


class Runner:
    """A fake subprocess.run: answers a canned result or raises."""

    def __init__(self, result: Any) -> None:
        self.result, self.calls = result, []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((argv, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def proc(code: int, out: str = "", err: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


def run_handler(
    runner: Runner,
    *,
    allowed: bool,
    host: str = "hp-gen8",
    loader_error: Exception | None = None,
    handler_class: type[HandlerLoaderRun] = HandlerLoaderRun,
) -> ModelLoaderRunResult:
    def loader_source() -> ModelRowsLoaderOverlay:
        if loader_error is not None:
            raise loader_error
        return ModelRowsLoaderOverlay(file_fallback_allowed=allowed, journal_env="J")

    settings = HandlerLoaderSettings(
        loader_source,
        lambda: HOSTS,
        InMemoryLoaderSettingsSink(),
        clock=lambda: datetime(2026, 11, 13, tzinfo=UTC),
    )
    return handler_class(settings, run=runner).handle(
        ModelLoaderRunRequest(registry_root="/r", host=host, loader_args=ARGS)
    )


def assert_flag_respected(result: ModelLoaderRunResult, allowed: bool) -> None:
    """fallback-allowed is only ever answered when the loader overlay allows the file fallback."""
    if result.status is EnumLoaderRunStatus.FALLBACK_ALLOWED:
        assert allowed
    if not allowed:
        assert result.status is not EnumLoaderRunStatus.FALLBACK_ALLOWED


def test_seam_the_exit_codes_the_plugin_reads_are_this_exact_table() -> None:
    assert (
        EXIT_RUN_ANSWERED,
        EXIT_RUN_FILE_FALLBACK_ANSWERED,
        EXIT_RUN_FALLBACK_ALLOWED,
        EXIT_RUN_UNREADABLE,
    ) == (0, 10, 11, 69)


@pytest.mark.parametrize("allowed", [True, False])
def test_golden_chain_the_loaders_rows_come_back_with_the_hosts_command_and_bound(
    allowed: bool,
) -> None:
    runner = Runner(proc(0, ROWS, "ROWS-LOADER ok\n"))
    result = run_handler(runner, allowed=allowed)
    assert result.status is EnumLoaderRunStatus.ANSWERED
    assert (result.stdout, result.stderr) == (ROWS, "ROWS-LOADER ok\n")
    ((argv, kwargs),) = runner.calls
    assert argv == ["loader", "-m", "x", *ARGS]
    assert kwargs["timeout"] == 45


def test_golden_chain_a_loader_that_read_the_file_itself_is_passed_through() -> None:
    result = run_handler(
        Runner(proc(10, ROWS, "ROWS-LOADER file-fallback\n")), allowed=True
    )
    assert result.status is EnumLoaderRunStatus.FILE_FALLBACK_ANSWERED
    assert result.stdout == ROWS


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        (Runner(subprocess.TimeoutExpired("loader", 45)), "loader-timeout"),
        (Runner(FileNotFoundError("loader")), "loader-not-runnable"),
        (Runner(proc(1, "", "ModuleNotFoundError")), "loader-failed-exit-1"),
        (Runner(proc(2, ROWS, "")), "loader-failed-exit-2"),
    ],
)
def test_error_chain_a_loader_with_no_answer_is_fallback_allowed_only_when_the_flag_is(
    runner: Runner, reason: str, allowed: bool
) -> None:
    result = run_handler(runner, allowed=allowed)
    assert_flag_respected(result, allowed)
    assert result.stdout == ""
    if allowed:
        assert (result.status, result.reason) == (
            EnumLoaderRunStatus.FALLBACK_ALLOWED,
            reason,
        )
    else:
        assert result.status is EnumLoaderRunStatus.UNREADABLE
        assert reason in result.reason
        assert "forbids the file fallback" in result.reason


@pytest.mark.parametrize("allowed", [True, False])
def test_error_chain_a_host_with_no_row_follows_the_flag(allowed: bool) -> None:
    runner = Runner(proc(0, ROWS))
    result = run_handler(runner, allowed=allowed, host="other")
    assert_flag_respected(result, allowed)
    assert runner.calls == []
    assert "loader-host-row-missing" in result.reason


@pytest.mark.parametrize("allowed", [True, False])
def test_error_chain_a_loader_that_reports_the_ledger_unreadable_is_unreadable_whatever_the_flag(
    allowed: bool,
) -> None:
    result = run_handler(
        Runner(proc(69, "", "ROWS-LOADER unreadable reason=node-failed\n")),
        allowed=allowed,
    )
    assert result.status is EnumLoaderRunStatus.UNREADABLE
    assert "unreadable reason=node-failed" in result.reason


def test_error_chain_a_missing_overlay_is_unreadable_naming_it_and_runs_nothing() -> (
    None
):
    runner = Runner(proc(0, ROWS))
    result = run_handler(
        runner,
        allowed=True,
        loader_error=NodeOverlayNotFoundError(
            "node_ledger_rows_loader_effect: the overlay file is not configured"
        ),
    )
    assert result.status is EnumLoaderRunStatus.UNREADABLE
    assert "node_ledger_rows_loader_effect" in result.reason
    assert runner.calls == []


def test_failing_control_a_handler_that_ignores_the_flag_fails_the_check() -> None:
    class Ignoring(HandlerLoaderRun):
        def _miss(
            self, settings: Any, reason: str, proc: Any = None
        ) -> ModelLoaderRunResult:
            return self._finish(
                settings, EnumLoaderRunStatus.FALLBACK_ALLOWED, reason, proc
            )

    runner = Runner(subprocess.TimeoutExpired("loader", 45))
    assert_flag_respected(
        run_handler(runner, allowed=False), False
    )  # the real handler passes
    broken = run_handler(runner, allowed=False, handler_class=Ignoring)
    with pytest.raises(AssertionError):
        assert_flag_respected(broken, False)


LOADER_SCRIPT = (
    "import sys\n"
    "print('{}'.format(' '.join(sys.argv[1:])))\n"
    "sys.exit(int(open(sys.argv[0] + '.code').read()))\n"
)


def cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    code: int | None,
    allowed: str = "true",
) -> tuple[int, str, str]:
    """Run the CLI in run mode against a real subprocess loader that exits with ``code`` (None: not runnable)."""
    tmp_path.mkdir(exist_ok=True)
    script = tmp_path / "fake_loader.py"
    script.write_text(LOADER_SCRIPT)
    (tmp_path / "fake_loader.py.code").write_text(str(code or 0))
    command = [
        sys.executable if code is not None else str(tmp_path / "missing"),
        str(script),
    ]
    env = write_overlays(
        tmp_path / "ovl",
        allowed=allowed,
        hosts=HEAD + host_row("hp-gen8", command),
    )
    exit_code = main(
        ["--registry-root", str(tmp_path), "--run", *ARGS],
        environ=env,
        hostname=lambda: "hp-gen8.lab",
    )
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def test_golden_cli_run_mode_passes_the_loaders_rows_and_arguments_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = cli(tmp_path, capsys, code=0)
    assert code == EXIT_RUN_ANSWERED
    assert out == "--reader drain-map --window-hours 24\n"
    assert err == ""


def test_golden_cli_run_mode_passes_a_loader_file_fallback_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _err = cli(tmp_path, capsys, code=10)
    assert code == EXIT_RUN_FILE_FALLBACK_ANSWERED
    assert out.startswith("--reader")


def test_error_cli_run_mode_a_failing_loader_is_exit_11_with_the_reason_last(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = cli(tmp_path, capsys, code=3)
    assert code == EXIT_RUN_FALLBACK_ALLOWED
    assert out == ""
    assert err.splitlines()[-1] == "loader-failed-exit-3"


def test_error_cli_run_mode_a_loader_that_cannot_run_is_exit_11_then_69_when_forbidden(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = cli(tmp_path, capsys, code=None)
    assert code == EXIT_RUN_FALLBACK_ALLOWED
    assert err.splitlines()[-1] == "loader-not-runnable"
    code, _out, err = cli(tmp_path / "again", capsys, code=None, allowed="false")
    assert code == EXIT_RUN_UNREADABLE
    assert "loader-not-runnable" in err.splitlines()[-1]
    assert "forbids the file fallback" in err.splitlines()[-1]


def test_error_cli_run_mode_a_missing_hosts_overlay_is_exit_69_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = write_overlays(tmp_path / "ovl")
    (tmp_path / "ovl" / "ledger_rows_loader_hosts" / "overlay.yaml").unlink()
    code = main(
        ["--registry-root", str(tmp_path), "--run", *ARGS],
        environ=env,
        hostname=lambda: "hp-gen8",
    )
    err = capsys.readouterr().err
    assert code == EXIT_RUN_UNREADABLE
    assert "ledger_rows_loader_hosts" in err.splitlines()[-1]


def test_loader_arguments_without_run_are_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            ["--registry-root", str(tmp_path), "--reader", "x"],
            environ=write_overlays(tmp_path / "ovl"),
        )
    assert raised.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
