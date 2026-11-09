# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain over the in-memory bus, error chains with the timer's exit codes, contract and overlay checks."""

from __future__ import annotations

import importlib
import json
import os
import stat
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_friction_rollup_tick_effect as node_package
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_core import (
    compute_trend,
    needs_ticket,
    previous_document,
    status_row,
    top_classes,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_io import (
    load_settings,
    prune_reports,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.handler_friction_rollup_tick import (
    DEFAULT_OVERLAY,
    HandlerFrictionRollupTick,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickRequest,
    ModelFrictionRollupTickResult,
    ModelFrictionRollupTickSettings,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_ledger_appender import (
    InMemoryLedgerAppender,
    SubprocessLedgerAppender,
)
from tests.nodes.node_friction_rollup_tick_effect.conftest import (
    CLASS_OVERLAY_ENV,
    OVERLAY_HOME_FILE,
)
from tests.nodes.node_friction_rollup_tick_effect.tick_support import (
    PARITY,
    install_ledger,
    observed,
    request_for,
    tick_diff,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.friction-rollup-tick-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.friction-rollup-tick-completed.v1"
NOW = "2026-09-26T12:00:00Z"
FIRST = next(c for c in PARITY["cases"] if c["name"] == "recorded-day-first-run")[
    "ticks"
][0]
DRY = next(c for c in PARITY["cases"] if c["name"] == "dry-run")["ticks"][0]


def _settings(**changes: object) -> ModelFrictionRollupTickSettings:
    document = yaml.safe_load(DEFAULT_OVERLAY.read_text(encoding="utf-8"))
    document.update(changes)
    return ModelFrictionRollupTickSettings.model_validate(document)


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


def _payload(tmp_path: Path, ledger: Path, **extra: object) -> dict[str, Any]:
    return {
        "registry_root": str(tmp_path / "registry"),
        "ledger_path": str(ledger),
        "now": NOW,
        "state_dir": str(tmp_path / "reports"),
        **extra,
    }


# --- golden chain ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_golden_chain_dry_run_over_bus_publishes_terminal(tmp_path: Path) -> None:
    ledger = install_ledger(tmp_path)
    runtime = await _run(tmp_path, _payload(tmp_path, ledger, dry_run=True))
    result = runtime.handler_result
    assert isinstance(result, ModelFrictionRollupTickResult)
    assert result.state == "DRY-RUN"
    assert result.exit_code == 0
    assert tick_diff(DRY, observed(result, tmp_path / "reports")) == []
    assert _terminal_status(tmp_path) == "success"
    assert not (tmp_path / "reports").exists()


@pytest.mark.asyncio
async def test_golden_chain_appends_the_timers_row_through_the_real_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_dir = tmp_path / "bin"
    _ledger_stub(stub_dir)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    ledger = install_ledger(tmp_path)
    runtime = await _run(tmp_path, _payload(tmp_path, ledger))
    result = runtime.handler_result
    assert isinstance(result, ModelFrictionRollupTickResult)
    assert (result.state, result.exit_code, result.status) == ("OK", 0, "success")
    assert (stub_dir / "appended.txt").read_text(encoding="utf-8").splitlines() == [
        FIRST["row"]
    ]
    assert tick_diff(FIRST, observed(result, tmp_path / "reports")) == []
    reports = sorted(p.name for p in (tmp_path / "reports").iterdir())
    assert reports == [
        "20260926T120000Z.json",
        "20260926T120000Z.md",
        "latest.json",
        "latest.md",
    ]
    assert _terminal_status(tmp_path) == "success"


# --- error chain ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_chain_unreadable_ledger_is_exit_2_and_a_failure_event(
    tmp_path: Path,
) -> None:
    runtime = await _run(
        tmp_path, _payload(tmp_path, tmp_path / "absent" / "ledger.md")
    )
    result = runtime.handler_result
    assert isinstance(result, ModelFrictionRollupTickResult)
    assert (result.exit_code, result.state, result.status) == (2, "REFUSED", "failure")
    assert "is absent" in result.error_message
    assert runtime.exit_code != 0
    assert _terminal_status(tmp_path) == "failure"


@pytest.mark.asyncio
async def test_error_chain_append_refused_is_exit_1_and_a_failure_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_dir = tmp_path / "bin"
    _ledger_stub(stub_dir, exit_code=7)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    ledger = install_ledger(tmp_path)
    runtime = await _run(tmp_path, _payload(tmp_path, ledger))
    result = runtime.handler_result
    assert isinstance(result, ModelFrictionRollupTickResult)
    assert (result.exit_code, result.state, result.status) == (
        1,
        "APPEND-FAILED",
        "failure",
    )
    assert "the STATUS row was refused: rc=7 refused-by-stub" in result.error_message
    assert "state=APPEND-FAILED" in result.log_line
    assert result.written  # the reports are written before the append, as the timer did
    assert _terminal_status(tmp_path) == "failure"


@pytest.mark.asyncio
async def test_error_chain_class_rules_absent_over_bus_is_exit_3_and_still_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CLASS_OVERLAY_ENV, str(tmp_path / "no-such-rules.yaml"))
    ledger = install_ledger(tmp_path)
    runtime = await _run(tmp_path, _payload(tmp_path, ledger, dry_run=True))
    result = runtime.handler_result
    assert isinstance(result, ModelFrictionRollupTickResult)
    assert (result.exit_code, result.status) == (3, "partial")
    assert "class-rules=unapplied:NodeOverlayError" in result.status_row
    assert runtime.exit_code == 0
    record = json.loads((tmp_path / "state" / "workflow_result.json").read_text())
    assert record["terminal_payload"]["exit_code"] == 3


def test_unreadable_ledger_input_is_exit_2(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_bytes(b"\xff\xfe not utf-8 \x80")
    result = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender()).handle(
        request_for(tmp_path, ledger, NOW)
    )
    assert (result.exit_code, result.state) == (2, "REFUSED")
    assert "input could not be read" in result.error_message


def test_bad_now_is_exit_2(tmp_path: Path) -> None:
    result = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender()).handle(
        request_for(tmp_path, install_ledger(tmp_path), "not-a-time")
    )
    assert result.exit_code == 2


def test_unwritable_state_dir_is_exit_2(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the state directory should be")
    request = request_for(tmp_path, install_ledger(tmp_path), NOW).model_copy(
        update={"state_dir": str(blocker / "state")}
    )
    result = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender()).handle(
        request
    )
    assert (result.exit_code, result.state) == (2, "REFUSED")
    assert "output could not be written" in result.error_message


def test_append_refused_with_the_in_memory_fake_is_exit_1(tmp_path: Path) -> None:
    fake = InMemoryLedgerAppender(refuse="rc=2 lint failed")
    result = HandlerFrictionRollupTick(appender=fake).handle(
        request_for(tmp_path, install_ledger(tmp_path), NOW)
    )
    assert result.exit_code == 1
    assert fake.rows == []
    assert result.error_message.endswith("rc=2 lint failed")


def test_append_command_that_does_not_run_is_refused(tmp_path: Path) -> None:
    appender = SubprocessLedgerAppender(["/nonexistent-ledger-command"], 5)
    outcome = appender.append(tmp_path / "ledger.md", "row")
    assert outcome.ok is False
    assert outcome.detail.startswith("ledger lock did not run:")


def test_successful_append_hands_the_row_to_the_fake(tmp_path: Path) -> None:
    fake = InMemoryLedgerAppender()
    ledger = install_ledger(tmp_path)
    result = HandlerFrictionRollupTick(appender=fake).handle(
        request_for(tmp_path, ledger, NOW)
    )
    assert fake.rows == [(ledger, FIRST["row"])]
    assert (result.exit_code, result.state) == (0, "OK")


def _unapplied_note_is_reported_everywhere(
    result: ModelFrictionRollupTickResult, cause: str, fake: InMemoryLedgerAppender
) -> None:
    assert (result.exit_code, result.state, result.status) == (3, "OK", "partial")
    note = f"class-rules=unapplied:{cause}"
    assert note in result.status_row
    assert result.status_row.split(" | ")[-2] == note
    assert result.log_line.endswith(note)
    assert result.class_rules == f"unapplied:{cause}"
    assert json.loads(result.report_json_text)["class_rules"] == f"unapplied:{cause}"
    assert fake.rows[0][1] == result.status_row
    assert " " not in note
    assert "|" not in note


def test_overlay_home_file_absent_is_exit_3_and_reported_everywhere(
    tmp_path: Path,
) -> None:
    fake = InMemoryLedgerAppender()
    handler = HandlerFrictionRollupTick(
        appender=fake,
        environ={CLASS_OVERLAY_ENV: str(tmp_path / "no-such-rules.yaml")},
    )
    result = handler.handle(request_for(tmp_path, install_ledger(tmp_path), NOW))
    _unapplied_note_is_reported_everywhere(result, "NodeOverlayError", fake)


def test_overlay_home_not_configured_is_exit_3_and_reported_everywhere(
    tmp_path: Path,
) -> None:
    fake = InMemoryLedgerAppender()
    handler = HandlerFrictionRollupTick(appender=fake, environ={})
    result = handler.handle(request_for(tmp_path, install_ledger(tmp_path), NOW))
    _unapplied_note_is_reported_everywhere(result, "NodeOverlayNotFoundError", fake)


def test_overlay_home_root_holds_the_rules_when_no_file_is_pinned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "overlay-home"
    (root / "node_friction_rollup_compute").mkdir(parents=True)
    (root / "node_friction_rollup_compute" / "overlay.yaml").write_text(
        OVERLAY_HOME_FILE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    handler = HandlerFrictionRollupTick(
        appender=InMemoryLedgerAppender(),
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
    )
    result = handler.handle(request_for(tmp_path, install_ledger(tmp_path), NOW))
    assert (result.exit_code, result.class_rules) == (0, "applied")


def test_class_rules_malformed_is_exit_3(tmp_path: Path) -> None:
    rules = tmp_path / "rules.yaml"
    rules.write_text("class_rules:\n  - {name: x, owner: y, pattern: '('}\n")
    handler = HandlerFrictionRollupTick(
        appender=InMemoryLedgerAppender(),
        environ={CLASS_OVERLAY_ENV: str(rules)},
    )
    result = handler.handle(request_for(tmp_path, install_ledger(tmp_path), NOW))
    assert result.exit_code == 3
    assert "class-rules=unapplied:ValueError" in result.status_row


def test_append_refused_and_rules_unapplied_is_exit_1(tmp_path: Path) -> None:
    handler = HandlerFrictionRollupTick(
        appender=InMemoryLedgerAppender(refuse="rc=1"),
        environ={CLASS_OVERLAY_ENV: str(tmp_path / "absent.yaml")},
    )
    result = handler.handle(request_for(tmp_path, install_ledger(tmp_path), NOW))
    assert result.exit_code == 1
    assert result.log_line.endswith("class-rules=unapplied:NodeOverlayError")


def test_class_rules_are_applied_to_rows_with_no_class(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "2026-09-26T10:00:00Z | FRICTION | lane=a | ticket=OMN-1 | cost=~5 min | lab alarm raised diskfull on host9\n"
        "2026-09-26T10:30:00Z | FRICTION | lane=b | ticket=OMN-1 | cost=~5 min | lab alarm raised diskfull on host9 again\n",
        encoding="utf-8",
    )
    result = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender()).handle(
        request_for(tmp_path, ledger, NOW)
    )
    assert result.exit_code == 0
    assert result.class_rules == "applied"
    assert "lab-alarm:diskfull" in result.status_row


# --- behaviour of the flow ------------------------------------------------------------------------------------


def test_dry_run_writes_nothing_and_appends_nothing(tmp_path: Path) -> None:
    fake = InMemoryLedgerAppender()
    result = HandlerFrictionRollupTick(appender=fake).handle(
        request_for(tmp_path, install_ledger(tmp_path), NOW, dry_run=True)
    )
    assert (result.exit_code, result.state, result.written) == (0, "DRY-RUN", ())
    assert fake.rows == []
    assert not (tmp_path / "state").exists()
    assert result.log_line.startswith(
        "friction-rollup 2026-09-26T12:00:00Z state=DRY-RUN"
    )


def test_state_dir_defaults_under_the_registry_root_and_archive_beside_the_ledger(
    tmp_path: Path,
) -> None:
    ledger = install_ledger(tmp_path)
    request = ModelFrictionRollupTickRequest(
        registry_root=str(tmp_path / "reg"), ledger_path=str(ledger), now=NOW
    )
    result = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender()).handle(
        request
    )
    state = tmp_path / "reg" / ".onex_state" / "friction-rollup"
    assert result.report_path == str(state / "20260926T120000Z.md")
    assert (state / "latest.json").is_file()
    sources = json.loads(result.report_json_text)["windows"]["24h"]["sources"]
    assert [s["path"] for s in sources] == [
        "ledger.md",
        "archive/ROLLING_WORK_LEDGER_2026-09-25-split.md",
    ]


def test_explicit_archive_dir_is_read(tmp_path: Path) -> None:
    ledger = install_ledger(tmp_path, archive=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "b-split.md").write_text(
        "2026-09-26T09:00:00Z | MSG | from=x | to=y | hello\n"
    )
    (elsewhere / "a-split.md").write_text(
        "2026-09-26T08:00:00Z | MSG | from=x | to=y | hello\n"
    )
    (elsewhere / "ignored.md").write_text(
        "2026-09-26T07:00:00Z | MSG | from=x | to=y | no\n"
    )
    request = request_for(tmp_path, ledger, NOW).model_copy(
        update={"archive_dir": str(elsewhere)}
    )
    result = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender()).handle(
        request
    )
    sources = json.loads(result.report_json_text)["windows"]["24h"]["sources"]
    assert [s["path"] for s in sources] == ["ledger.md", "a-split.md", "b-split.md"]


def test_prune_removes_old_stamped_reports_and_keeps_latest(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    for name in (
        "20260901T000000Z.json",
        "20260901T000000Z.md",
        "20260925T000000Z.json",
        "latest.json",
        "latest.md",
        "notes.txt",
    ):
        (state / name).write_text("x")
    removed = prune_reports(state, datetime(2026, 9, 26, 12, tzinfo=UTC), 7)
    assert [p.name for p in removed] == ["20260901T000000Z.json", "20260901T000000Z.md"]
    assert sorted(p.name for p in state.iterdir()) == [
        "20260925T000000Z.json",
        "latest.json",
        "latest.md",
        "notes.txt",
    ]


def test_tick_prunes_through_the_handler_with_the_overlay_keep_days(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "20260801T000000Z.json").write_text("x")
    (state / "20260926T000000Z.json").write_text("x")
    result = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender()).handle(
        request_for(tmp_path, install_ledger(tmp_path), NOW)
    )
    assert result.pruned == ("20260801T000000Z.json",)
    assert (state / "20260926T000000Z.json").is_file()


def test_previous_tick_that_is_foreign_or_unreadable_is_a_first_run(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    ledger = install_ledger(tmp_path)
    handler = HandlerFrictionRollupTick(appender=InMemoryLedgerAppender())
    for text in (
        "not json",
        json.dumps({"schema": "other/v1", "windows": {}}),
        json.dumps(["x"]),
    ):
        (state / "latest.json").write_text(text)
        result = handler.handle(request_for(tmp_path, ledger, NOW))
        assert " | trend=first run | " in result.status_row
    settings = _settings()
    assert (
        previous_document(
            {"schema": settings.schema_name, "windows": {"24h": {}}}, settings
        )
        is None
    )
    assert (
        previous_document(
            {"schema": settings.schema_name, "windows": {"24h": {}, "2h": {}}}, settings
        )
        is not None
    )


def test_pure_helpers_on_empty_report() -> None:
    settings = _settings()
    empty: dict[str, object] = {
        "classes": [],
        "totals": {},
        "window": {"since": "s", "until": "u"},
    }
    current = {"24h": empty, "2h": empty}
    top = top_classes(empty, settings.top_n)
    flagged = needs_ticket(empty)
    trend = compute_trend(current, top, flagged, None, "", settings)
    assert (top, flagged, trend) == ([], [], {"previous": None})
    summary = {"top_classes": top, "needs_ticket": flagged, "trend": trend}
    row = status_row(
        datetime(2026, 9, 26, tzinfo=UTC), current, summary, "r.md", settings
    )
    assert "top5=none | trend=first run | needs-ticket=none | report=" in row


def test_status_row_is_cut_at_the_overlay_budget() -> None:
    settings = _settings(max_row_chars=300)
    empty: dict[str, object] = {"classes": [], "totals": {}, "window": {}}
    row = status_row(
        datetime(2026, 9, 26, tzinfo=UTC),
        {"24h": empty, "2h": empty},
        {"top_classes": [], "needs_ticket": [], "trend": {"previous": None}},
        "r.md",
        settings,
    )
    assert len(row) == 300
    assert row.endswith("...")


# --- contract, registration, overlay --------------------------------------------------------------------------


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]
    assert (
        contract["handler_routing"]["handlers"][0]["operation"]
        == "run_friction_rollup_tick"
    )
    assert set(contract["outputs"]) == set(ModelFrictionRollupTickResult.model_fields)
    assert set(contract["inputs"]) == set(ModelFrictionRollupTickRequest.model_fields)
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeFrictionRollupTickEffect, handler_type)
    for side, model in [
        ("input_model", ModelFrictionRollupTickRequest),
        ("output_model", ModelFrictionRollupTickResult),
    ]:
        assert (
            getattr(
                importlib.import_module(contract[side]["module"]),
                contract[side]["name"],
            )
            is model
        )
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_friction_rollup_tick_effect"].load() is node_package
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["entry_points"]["onex.nodes"] == {
        "node_friction_rollup_tick_effect": "omnimarket.nodes.node_friction_rollup_tick_effect"
    }


def test_overlay_holds_every_setting_and_the_code_holds_none() -> None:
    overlay = yaml.safe_load(DEFAULT_OVERLAY.read_text(encoding="utf-8"))
    fields = {
        f.alias or name
        for name, f in ModelFrictionRollupTickSettings.model_fields.items()
    }
    assert set(overlay) == fields
    settings = load_settings(DEFAULT_OVERLAY)
    assert (
        settings.top_n,
        settings.keep_days,
        settings.max_row_chars,
        settings.bucket_hours,
    ) == (5, 7, 3000, 2)
    assert [(w.name, w.hours) for w in settings.windows] == [("24h", 24), ("2h", 2)]
    assert settings.umbrella_ticket == "OMN-17427"
    assert settings.ticket == "OMN-19715"
    assert settings.appender_timeout_seconds == 60
    for path in (NODE_DIR / "handlers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for literal in (
            "OMN-19715",
            "OMN-17427",
            "friction-rollup-timer",
            "launchd:friction-rollup",
            ".onex_state",
            "onex-ledger",
            "*-split.md",
        ):
            assert literal not in text, f"{literal} is hard-coded in {path.name}"


def test_request_needs_only_the_registry_root_and_the_ledger() -> None:
    request = ModelFrictionRollupTickRequest.model_validate(
        {"registry_root": "r", "ledger_path": "l"}
    )
    assert (request.now, request.dry_run, request.state_dir, request.archive_dir) == (
        None,
        False,
        None,
        None,
    )
    with pytest.raises(ValidationError):
        ModelFrictionRollupTickRequest.model_validate({"registry_root": "r"})
    with pytest.raises(ValidationError):
        ModelFrictionRollupTickRequest.model_validate(
            {"registry_root": "r", "ledger_path": "l", "surprise": 1}
        )
    with pytest.raises(ValidationError, match="frozen"):
        request.dry_run = True


def test_node_files_name_no_home_path_and_no_person() -> None:
    for path in NODE_DIR.rglob("*"):
        if path.suffix in {".py", ".yaml"}:
            text = path.read_text(encoding="utf-8")
            assert "/home/" not in text, path.name
            assert "/Us" + "ers/" not in text, path.name
            assert "omni" + "_home" not in text, path.name


def test_workflow_result_enum_is_the_runtime_vocabulary() -> None:
    assert EnumWorkflowResult.FAILED.value == "failed"
