# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain and error chain of the parity receipt writer over the in-memory bus (OMN-23251), the skip rule, the move's
parity with the writer it replaces (OMN-20536) and the contract and overlay checks."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_work_ledger_parity_receipt_effect as node_package
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    InMemoryLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.handlers.handler_parity_receipt import (
    DEFAULT_OVERLAY,
    HandlerParityReceipt,
    load_settings,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.handlers.parity_receipt_core import (
    build_dsn,
    receipt_owed,
    receipt_rows,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.models.model_parity_receipt import (
    ModelParityReceiptResult,
    ModelParityReceiptSettings,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.protocols.protocol_parity_check import (
    InMemoryParityCheck,
    ModelRunOutcome,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.runtime_parity_receipt import (
    main,
)
from tests.nodes.node_work_ledger_parity_receipt_effect.support import (
    CLEAN,
    DAY,
    FRESH,
    NOW,
    UNEXPLAINED,
    UNHEALTHY,
    request_for,
    write_world,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
NODE_DIR = Path(node_package.__file__).parent
LANE = "work-ledger-parity"
TERMINAL_TOPIC = "onex.evt.omnimarket.work-ledger-parity-receipt-completed.v1"


def _handler(
    runner: InMemoryParityCheck | None = None,
) -> tuple[HandlerParityReceipt, InMemoryLedgerAppender]:
    appender = InMemoryLedgerAppender()
    return HandlerParityReceipt(
        runner=runner or InMemoryParityCheck(), appender=appender
    ), appender


def _ledger_stub(directory: Path, exit_code: int = 0) -> Path:
    """An ``onex-ledger`` on PATH that records the row it is asked to append and exits with exit_code."""
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / "onex-ledger"
    stub.write_text(
        "#!/bin/sh\n"
        '[ "$2" = "--append" ] || exit 64\n'
        f'printf "%s\\n" "$3" >> "{directory}/appended.txt"\n'
        f"echo refused-by-stub >&2\nexit {exit_code}\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


async def _run(
    tmp_path: Path, payload: dict[str, Any], name: str = "state"
) -> RuntimeLocal:
    input_path = tmp_path / f"{name}-request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / name,
        backend_overrides={"event_bus": "inmemory"},
        timeout=30,
    )
    await runtime.run_async()
    return runtime


def _terminal_status(tmp_path: Path, name: str = "state") -> str:
    record = json.loads((tmp_path / name / "workflow_result.json").read_text())
    return str(record["terminal_payload"]["status"])


def _payload(tmp_path: Path, ledger: Path, fields: dict[str, str]) -> dict[str, Any]:
    return {
        "registry_root": str(tmp_path / "registry"),
        "ledger_path": str(ledger),
        "now": NOW,
        **fields,
    }


# --- golden chain ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_golden_chain_planted_missing_receipt_gets_a_fresh_receipt_over_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_dir = tmp_path / "bin"
    _ledger_stub(stub_dir)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    ledger, fields = write_world(tmp_path, [UNHEALTHY])
    runtime = await _run(tmp_path, _payload(tmp_path, ledger, fields))
    result = runtime.handler_result
    assert isinstance(result, ModelParityReceiptResult)
    assert (result.state, result.reason, result.exit_code, result.status) == (
        "APPENDED", "newest-unhealthy", 0, "success",
    )  # fmt: skip
    assert (stub_dir / "appended.txt").read_text(encoding="utf-8").splitlines() == [
        FRESH
    ]
    assert _terminal_status(tmp_path) == "success"


@pytest.mark.asyncio
async def test_golden_chain_planted_clean_receipt_is_skipped_over_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_dir = tmp_path / "bin"
    _ledger_stub(stub_dir)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    ledger, fields = write_world(tmp_path, [CLEAN])
    runtime = await _run(tmp_path, _payload(tmp_path, ledger, fields))
    result = runtime.handler_result
    assert isinstance(result, ModelParityReceiptResult)
    assert (result.state, result.reason, result.exit_code) == (
        "PRESENT",
        "newest-clean",
        0,
    )
    assert not (stub_dir / "appended.txt").exists()
    assert not (tmp_path / "parity-argv.txt").exists(), (
        "the parity check is not run for a clean day"
    )
    assert _terminal_status(tmp_path) == "success"


@pytest.mark.asyncio
async def test_golden_chain_runs_the_parity_check_for_the_previous_day_with_the_built_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_dir = tmp_path / "bin"
    _ledger_stub(stub_dir)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    ledger, fields = write_world(tmp_path, [])
    await _run(tmp_path, _payload(tmp_path, ledger, fields))
    argv = (tmp_path / "parity-argv.txt").read_text(encoding="utf-8").splitlines()
    assert argv[:2] == ["-m", "omnimarket.nodes.node_projection_work_ledger.parity"]
    assert argv[argv.index("--utc-day") + 1] == DAY
    assert "--receipt" in argv
    assert argv[argv.index("--dsn-env") + 1] == "WL_DSN"
    assert (tmp_path / "parity-dsn.txt").read_text(encoding="utf-8").strip() == (
        "postgresql://postgres:s3cret@db.lab.example:5436/analytics"
    )


# --- error chain ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_chain_unreadable_ledger_is_exit_1_and_a_failure_event(
    tmp_path: Path,
) -> None:
    _, fields = write_world(tmp_path, [])
    runtime = await _run(
        tmp_path, _payload(tmp_path, tmp_path / "absent" / "ledger.md", fields)
    )
    result = runtime.handler_result
    assert isinstance(result, ModelParityReceiptResult)
    assert (result.exit_code, result.state, result.status) == (1, "FAILED", "failure")
    assert "cannot read the ledger" in result.error_message
    assert runtime.exit_code != 0
    assert _terminal_status(tmp_path) == "failure"


@pytest.mark.asyncio
async def test_error_chain_append_refused_is_exit_1_and_a_failure_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_dir = tmp_path / "bin"
    _ledger_stub(stub_dir, exit_code=7)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    ledger, fields = write_world(tmp_path, [UNHEALTHY])
    runtime = await _run(tmp_path, _payload(tmp_path, ledger, fields))
    result = runtime.handler_result
    assert isinstance(result, ModelParityReceiptResult)
    assert (result.exit_code, result.state, result.status) == (1, "FAILED", "failure")
    assert (
        "onex-ledger refused the receipt (rc=7 refused-by-stub)" in result.error_message
    )
    assert _terminal_status(tmp_path) == "failure"


@pytest.mark.asyncio
async def test_error_chain_unconfigured_host_is_exit_1_not_configured_over_the_bus(
    tmp_path: Path,
) -> None:
    ledger, _ = write_world(tmp_path, [UNHEALTHY])
    runtime = await _run(tmp_path, _payload(tmp_path, ledger, {}))
    result = runtime.handler_result
    assert isinstance(result, ModelParityReceiptResult)
    assert (result.exit_code, result.state) == (1, "NOT-CONFIGURED")
    assert (
        "parity_python, dsn_overlay_path, dsn_env_file, pg_port unset"
        in result.error_message
    )
    assert _terminal_status(tmp_path) == "failure"


# --- the skip rule (OMN-23251) --------------------------------------------------------------------------------


def test_planted_missing_receipt_gets_a_new_receipt() -> None:
    """The failing control: the writer it replaces skipped any day holding a receipt, so this was never appended."""
    assert receipt_owed(f"{UNHEALTHY}\n", LANE, DAY) == "newest-unhealthy"


def test_planted_clean_receipt_gets_no_new_receipt() -> None:
    assert receipt_owed(f"{CLEAN}\n", LANE, DAY) == "newest-clean"


def test_planted_unexplained_receipt_gets_a_new_receipt() -> None:
    assert receipt_owed(f"{UNEXPLAINED}\n", LANE, DAY) == "newest-unhealthy"


def test_a_day_with_no_receipt_is_owed_one_and_another_days_receipt_does_not_count() -> (
    None
):
    other = CLEAN.replace("day=2026-10-04", "day=2026-10-03")
    assert receipt_owed(f"{other}\n", LANE, DAY) == "no-receipt"
    assert receipt_owed("", LANE, DAY) == "no-receipt"


def test_only_the_newest_receipt_of_the_day_decides() -> None:
    later_clean = CLEAN.replace("2026-10-05T00:40:07Z", "2026-10-05T02:00:00Z")
    assert receipt_owed(f"{UNHEALTHY}\n{later_clean}\n", LANE, DAY) == "newest-clean"
    assert receipt_owed(f"{later_clean}\n{UNHEALTHY}\n", LANE, DAY) == "newest-clean"
    later_bad = UNHEALTHY.replace("2026-10-05T00:40:07Z", "2026-10-05T02:00:00Z")
    assert receipt_owed(f"{CLEAN}\n{later_bad}\n", LANE, DAY) == "newest-unhealthy"


def test_a_receipt_that_shows_no_counts_proves_nothing() -> None:
    bare = "2026-10-05T00:40:07Z | STATUS | lane=work-ledger-parity | actor=x | day=2026-10-04 | text"
    assert receipt_owed(f"{bare}\n", LANE, DAY) == "newest-unhealthy"


def test_another_lane_row_for_the_day_is_not_a_receipt() -> None:
    other = CLEAN.replace("lane=work-ledger-parity", "lane=other")
    assert receipt_rows(f"{other}\n", LANE) == []


def test_the_handler_appends_for_a_missing_receipt_and_writes_nothing_for_a_clean_one(
    tmp_path: Path,
) -> None:
    ledger, fields = write_world(tmp_path, [UNHEALTHY])
    runner = InMemoryParityCheck(ModelRunOutcome(returncode=1, stdout=FRESH + "\n"))
    handler, appender = _handler(runner)
    result = handler.handle(request_for(tmp_path, ledger, fields))
    assert result.state == "APPENDED"
    assert result.receipt_row == FRESH
    assert (
        result.log_line == f"parity receipt: APPENDED day={DAY} reason=newest-unhealthy"
    )
    assert appender.rows == [(ledger, FRESH)]

    clean_ledger = tmp_path / "clean.md"
    clean_ledger.write_text(f"{CLEAN}\n", encoding="utf-8")
    runner2 = InMemoryParityCheck()
    handler2, appender2 = _handler(runner2)
    skipped = handler2.handle(request_for(tmp_path, clean_ledger, fields))
    assert skipped.state == "PRESENT"
    assert appender2.rows == []
    assert runner2.calls == []


def test_dry_run_reports_what_is_owed_and_runs_nothing(tmp_path: Path) -> None:
    ledger, fields = write_world(tmp_path, [UNHEALTHY])
    runner = InMemoryParityCheck()
    handler, appender = _handler(runner)
    result = handler.handle(request_for(tmp_path, ledger, fields, dry_run=True))
    assert (result.state, result.reason, result.day) == (
        "DRY-RUN",
        "newest-unhealthy",
        DAY,
    )
    assert appender.rows == []
    assert runner.calls == []


def test_a_receipt_the_real_parity_writer_prints_is_read_back_by_the_skip_rule() -> (
    None
):
    from datetime import UTC, datetime

    from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
        EnumParityMismatchKind,
        ModelParityMismatch,
        ModelWorkLedgerParityReport,
    )
    from omnimarket.nodes.node_projection_work_ledger.parity_receipt import receipt_row

    now = datetime(2026, 11, 9, 0, 40, 29, tzinfo=UTC)
    window = {
        "window_since": datetime(2026, 11, 8, tzinfo=UTC),
        "window_until": datetime(2026, 11, 9, tzinfo=UTC),
        "state_entities_compared": 0,
        "unemittable_rows": 0,
    }
    clean = ModelWorkLedgerParityReport(file_rows=3, projection_rows=3, **window)
    missing = ModelWorkLedgerParityReport(
        file_rows=3,
        projection_rows=2,
        mismatches=(
            ModelParityMismatch(
                kind=EnumParityMismatchKind.ROW_MISSING_IN_PROJECTION,
                key="f04a5882cc20",
                row_type="STATUS",
            ),
        ),
        **window,
    )
    assert (
        receipt_owed(receipt_row(clean, "2026-11-08", now) + "\n", LANE, "2026-11-08")
        == "newest-clean"
    )
    row = receipt_row(missing, "2026-11-08", now)
    assert "| missing=1 |" in row
    assert receipt_owed(row + "\n", LANE, "2026-11-08") == "newest-unhealthy"


# --- parity with the writer it replaces (OMN-20536) -----------------------------------------------------------


def test_before_0015z_nothing_is_run(tmp_path: Path) -> None:
    ledger, fields = write_world(tmp_path, [])
    runner = InMemoryParityCheck()
    handler, _ = _handler(runner)
    result = handler.handle(
        request_for(tmp_path, ledger, fields, now="2026-10-05T00:10:00Z")
    )
    assert result.state == "NOT-YET"
    assert runner.calls == []


@pytest.mark.parametrize(
    ("rc", "out"),
    [
        (2, ""),
        (0, "not a receipt row\n"),
        (0, CLEAN.replace("day=2026-10-04", "day=2026-10-03") + "\n"),
        (0, CLEAN + "\n" + CLEAN + "\n"),
    ],
)
def test_a_bad_parity_answer_is_never_appended(
    tmp_path: Path, rc: int, out: str
) -> None:
    ledger, fields = write_world(tmp_path, [])
    handler, appender = _handler(
        InMemoryParityCheck(ModelRunOutcome(returncode=rc, stdout=out))
    )
    result = handler.handle(request_for(tmp_path, ledger, fields))
    assert (result.state, result.exit_code) == ("FAILED", 1)
    assert appender.rows == []


def test_an_inexact_day_is_appended_as_the_parity_check_printed_it(
    tmp_path: Path,
) -> None:
    ledger, fields = write_world(tmp_path, [])
    handler, appender = _handler(
        InMemoryParityCheck(ModelRunOutcome(returncode=1, stdout=UNHEALTHY + "\n"))
    )
    assert handler.handle(request_for(tmp_path, ledger, fields)).state == "APPENDED"
    assert appender.rows[-1][1] == UNHEALTHY


def test_a_parity_check_that_cannot_run_is_a_failure_naming_it(tmp_path: Path) -> None:
    ledger, fields = write_world(tmp_path, [])
    handler, appender = _handler(InMemoryParityCheck(fail="no such interpreter"))
    result = handler.handle(request_for(tmp_path, ledger, fields))
    assert result.state == "FAILED"
    assert "parity check did not run" in result.error_message
    assert appender.rows == []


def test_the_dsn_is_built_from_the_overlay_the_env_file_and_the_port() -> None:
    settings = load_settings(DEFAULT_OVERLAY)
    dsn = build_dsn(
        'ssh_host_env: LAB_HOST\ndatabase: "analytics"\ndb_user: postgres\n',
        "export LAB_HOST=someone@db.lab.example\nPOSTGRES_PASSWORD='s3cret'\n",
        {},
        5436,
        settings,
    )
    assert dsn == "postgresql://postgres:s3cret@db.lab.example:5436/analytics"
    from_environment = build_dsn(
        'ssh_host_env: LAB_HOST\ndatabase: "analytics"\ndb_user: postgres\n',
        "LAB_HOST=a@other.lab.example\nPOSTGRES_PASSWORD=p\n",
        {"LAB_HOST": "me@alt.lab.example"},
        5436,
        settings,
    )
    assert "@alt.lab.example:5436/" in from_environment


# --- the unit's entry ------------------------------------------------------------------------------------------


def test_main_appends_through_the_ledger_command_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, fields = write_world(tmp_path, [UNHEALTHY])
    recorded = tmp_path / "appended.txt"
    stub = tmp_path / "stub_ledger.py"
    stub.write_text(
        f"import sys\nopen({str(recorded)!r}, 'a').write(sys.argv[3] + '\\n')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_LEDGER_COMMAND", f"{sys.executable} {stub}")
    monkeypatch.setenv("ONEX_LEDGER_PARITY_PYTHON", fields["parity_python"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_OVERLAY", fields["dsn_overlay_path"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_ENV_FILE", fields["dsn_env_file"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_PG_PORT", fields["pg_port"])
    assert (
        main(
            [
                "--omni-home",
                str(tmp_path / "registry"),
                "--ledger",
                str(ledger),
                "--now",
                NOW,
            ]
        )
        == 0
    )
    assert recorded.read_text(encoding="utf-8").splitlines() == [FRESH]
    assert "parity receipt: APPENDED" in capsys.readouterr().out


def test_main_exits_1_when_the_host_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = write_world(tmp_path, [UNHEALTHY])
    for name in (
        "ONEX_LEDGER_PARITY_PYTHON", "ONEX_LEDGER_PARITY_OVERLAY",
        "ONEX_LEDGER_PARITY_ENV_FILE", "ONEX_LEDGER_PARITY_PG_PORT",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    assert (
        main(
            [
                "--omni-home",
                str(tmp_path / "registry"),
                "--ledger",
                str(ledger),
                "--now",
                NOW,
            ]
        )
        == 1
    )


def test_main_reads_the_ledger_from_the_environment_and_writes_its_evidence_in_the_state_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The host unit passes only --omni-home and --state-dir (the registry module tick template, OMN-23258)."""
    ledger, fields = write_world(tmp_path, [UNHEALTHY])
    recorded = tmp_path / "appended.txt"
    stub = tmp_path / "stub_ledger.py"
    stub.write_text(
        f"import sys\nopen({str(recorded)!r}, 'a').write(sys.argv[3] + '\\n')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_LEDGER_PARITY_FILE", str(ledger))
    monkeypatch.setenv("ONEX_LEDGER_COMMAND", f"{sys.executable} {stub}")
    monkeypatch.setenv("ONEX_LEDGER_PARITY_PYTHON", fields["parity_python"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_OVERLAY", fields["dsn_overlay_path"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_ENV_FILE", fields["dsn_env_file"])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_PG_PORT", fields["pg_port"])
    state_dir = tmp_path / "evidence"
    assert (
        main(
            [
                "--omni-home",
                str(tmp_path / "registry"),
                "--state-dir",
                str(state_dir),
                "--now",
                NOW,
            ]
        )
        == 0
    )
    assert recorded.read_text(encoding="utf-8").splitlines() == [FRESH]
    evidence = json.loads((state_dir / "last-run.json").read_text(encoding="utf-8"))
    assert evidence["exit_code"] == 0
    assert evidence["day"] == DAY
    assert "parity receipt: APPENDED" in capsys.readouterr().out


def test_main_writes_no_evidence_when_the_host_is_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = write_world(tmp_path, [UNHEALTHY])
    monkeypatch.setenv("ONEX_LEDGER_PARITY_FILE", str(ledger))
    for name in (
        "ONEX_LEDGER_PARITY_PYTHON", "ONEX_LEDGER_PARITY_OVERLAY",
        "ONEX_LEDGER_PARITY_ENV_FILE", "ONEX_LEDGER_PARITY_PG_PORT",
    ):  # fmt: skip
        monkeypatch.delenv(name, raising=False)
    state_dir = tmp_path / "evidence"
    assert (
        main(
            [
                "--omni-home",
                str(tmp_path / "registry"),
                "--state-dir",
                str(state_dir),
                "--now",
                NOW,
            ]
        )
        == 1
    )
    assert not (state_dir / "last-run.json").exists()


def test_main_refuses_a_relative_or_missing_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_PARITY_FILE", raising=False)
    for argv in (
        ["--omni-home", str(tmp_path)],
        ["--omni-home", str(tmp_path), "--ledger", "ledger.md"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            main(argv)
        assert excinfo.value.code == 2


# --- contract and overlay --------------------------------------------------------------------------------------


def test_the_contract_declares_the_command_and_terminal_topics_and_the_handler() -> (
    None
):
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        contract["runtime_dispatch"]["command_topic"]
    ]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["handler"]["class"] == "HandlerParityReceipt"


def test_the_overlay_holds_every_setting_and_an_unknown_one_is_refused() -> None:
    document = yaml.safe_load(DEFAULT_OVERLAY.read_text(encoding="utf-8"))
    settings = ModelParityReceiptSettings.model_validate(document)
    assert (settings.lane, settings.not_before_hour, settings.not_before_minute) == (
        LANE,
        0,
        15,
    )
    with pytest.raises(ValidationError):
        ModelParityReceiptSettings.model_validate({**document, "extra_key": 1})
    missing = {k: v for k, v in document.items() if k != "lane"}
    with pytest.raises(ValidationError):
        ModelParityReceiptSettings.model_validate(missing)
