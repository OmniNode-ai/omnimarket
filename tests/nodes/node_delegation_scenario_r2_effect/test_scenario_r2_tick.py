# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scenario R2 tick the registry installer renders (OMN-23432).

The tick runs the scenario once as a SCHEDULE run. The golden chain does it with the scripted
onex of the golden chain test and a fake store that supplies every setting: exit 0, the record
is success with no unresolved setting, one STATUS row, and the key value nowhere. The error
chains are the named breaks: a setting the environment already holds is never asked of the
store, a store that supplies nothing fails the run (never skips it), a store that raises for
one name leaves only that name unresolved, and a missing ledger route refuses with 65.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_runtime import InMemoryScenarioEventSink
from omnimarket.models.model_delegation_scenario import EnumScenarioTrigger
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2 import (
    HandlerScenarioR2,
    load_schedule,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.handlers.handler_scenario_r2_settings import (
    HandlerSecretStoreSettings,
    setting_names,
)
from omnimarket.nodes.node_delegation_scenario_r2_effect.runtime_scenario_r2_tick import (
    main,
)
from tests.nodes.node_delegation_scenario_r2_effect.test_golden_chain_scenario_r2_effect import (
    KEY,
    RUNTIME,
    SETTINGS,
    ScriptedOnex,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 9, 13, 6, 30, tzinfo=UTC)
ALL_NAMES = tuple(SETTINGS)


class FakeSettingStore:
    """The store seam for the tick: the values it holds, the names it is asked, the name that raises."""

    def __init__(self, values: dict[str, str], *, raise_for: str | None = None) -> None:
        self._values = {name: SecretStr(value) for name, value in values.items()}
        self._raise_for = raise_for
        self.asked: list[str] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        assert folder == RUNTIME.secret_store_folder
        self.asked.append(name)
        if name == self._raise_for:
            raise RuntimeError("the store refused this name")
        return self._values.get(name)


class _Factory:
    """The runner factory for the tests: the scripted onex, the rows, the in-memory sink."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self.handlers: list[HandlerScenarioR2] = []
        self.runners: list[ScriptedOnex] = []

    def __call__(
        self, environment: Mapping[str, str], trigger: EnumScenarioTrigger
    ) -> HandlerScenarioR2:
        runner = ScriptedOnex()
        handler = HandlerScenarioR2(
            RUNTIME,
            home=Path(environment[RUNTIME.home_env]),
            environment=environment,
            runner=runner,
            appender=self.rows.append,
            sink=InMemoryScenarioEventSink(),
            trigger=trigger,
            clock=lambda: NOW,
        )
        self.handlers.append(handler)
        self.runners.append(runner)
        return handler


def _clear_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_NAMES:
        monkeypatch.delenv(name, raising=False)


def _record_text(state_dir: Path) -> str:
    return (state_dir / "last-run.json").read_text(encoding="utf-8")


def _tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory: _Factory,
    store: FakeSettingStore,
) -> tuple[int, Path]:
    _clear_environment(monkeypatch)
    state_dir = tmp_path / "state"
    exit_code = main(
        ["--omni-home", str(tmp_path / "home"), "--state-dir", str(state_dir)],
        store=store,
        runner_factory=factory,
    )
    return exit_code, state_dir


def test_golden_tick_fills_the_settings_runs_and_writes_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = _Factory()
    store = FakeSettingStore(SETTINGS)
    exit_code, state_dir = _tick(tmp_path, monkeypatch, factory, store)
    assert exit_code == 0
    record = json.loads(_record_text(state_dir))
    assert record["scenario"] == RUNTIME.scenario_id
    assert record["conclusion"] == "success"
    assert record["unresolved_settings"] == []
    assert "ran_at" in record
    # The ledger append route got exactly one STATUS row for this run.
    assert len(factory.rows) == 1
    assert "| STATUS |" in factory.rows[0]
    assert "conclusion=success" in factory.rows[0]
    assert "trigger=schedule" in factory.rows[0]
    captured = capsys.readouterr()
    assert json.loads(captured.out)["conclusion"] == "success"
    # The key value appears in neither stdout, stderr, the record, nor the row.
    for text in (captured.out, captured.err, _record_text(state_dir), *factory.rows):
        assert KEY not in text


def test_tick_never_asks_the_store_for_a_setting_the_environment_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.setenv(
        RUNTIME.onboarding.provider_env, SETTINGS[RUNTIME.onboarding.provider_env]
    )
    monkeypatch.setenv(RUNTIME.onboarding.key_env, KEY)
    factory = _Factory()
    store = FakeSettingStore(SETTINGS)
    state_dir = tmp_path / "state"
    exit_code = main(
        ["--omni-home", str(tmp_path / "home"), "--state-dir", str(state_dir)],
        store=store,
        runner_factory=factory,
    )
    assert exit_code == 0
    assert RUNTIME.onboarding.provider_env not in store.asked
    assert RUNTIME.onboarding.key_env not in store.asked
    held = {RUNTIME.onboarding.provider_env, RUNTIME.onboarding.key_env}
    assert sorted(store.asked) == sorted(name for name in ALL_NAMES if name not in held)


def test_tick_a_store_that_supplies_nothing_fails_the_run_and_never_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _Factory()
    store = FakeSettingStore({})
    exit_code, state_dir = _tick(tmp_path, monkeypatch, factory, store)
    assert exit_code == 1
    record = json.loads(_record_text(state_dir))
    assert record["conclusion"] == "failure"
    assert record["unresolved_settings"] == list(setting_names(RUNTIME))
    assert factory.rows
    assert "conclusion=failure" in factory.rows[0]
    # A missing setting is a failure that runs no command.
    assert factory.runners[0].calls == []
    # The record holds the names, never a value.
    for value in SETTINGS.values():
        assert value not in _record_text(state_dir)


def test_tick_a_store_that_raises_for_one_name_leaves_only_that_name_unresolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raised = RUNTIME.onboarding.key_env
    factory = _Factory()
    store = FakeSettingStore(SETTINGS, raise_for=raised)
    exit_code, state_dir = _tick(tmp_path, monkeypatch, factory, store)
    assert exit_code == 1
    record = json.loads(_record_text(state_dir))
    assert record["conclusion"] == "failure"
    assert record["unresolved_settings"] == [raised]
    # The raising name is unresolved; the others were still filled.
    handler = factory.handlers[0]
    for name, value in SETTINGS.items():
        if name != raised:
            assert handler._environment[name] == value


def test_tick_a_missing_ledger_route_refuses_with_65(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _clear_environment(monkeypatch)
    monkeypatch.delenv(RUNTIME.ledger_append_command_env, raising=False)
    store = FakeSettingStore(SETTINGS)
    state_dir = tmp_path / "state"
    exit_code = main(
        ["--omni-home", str(tmp_path / "home"), "--state-dir", str(state_dir)],
        store=store,
    )
    assert exit_code == 65
    record = json.loads(_record_text(state_dir))
    assert record["conclusion"] == "refused"
    assert RUNTIME.ledger_append_command_env in record["refused"]
    captured = capsys.readouterr()
    assert RUNTIME.ledger_append_command_env in captured.err


def test_setting_names_are_the_schedule_names_plus_the_key_the_environment_file_carries() -> (
    None
):
    """Seam: the tick's settings and the schedule's unit environment change together, the key apart."""
    names = set(setting_names(RUNTIME))
    scheduled = {
        name
        for name in load_schedule().unit_environment
        if name.startswith("ONEX_SCENARIO_R2_")
    }
    assert len(names) == 7
    assert names == scheduled | {RUNTIME.onboarding.key_env}
    assert RUNTIME.onboarding.key_env not in scheduled


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0


def test_a_machine_with_no_store_overlay_and_no_identity_file_supplies_nothing(
    tmp_path: Path,
) -> None:
    """Error chain on the real store seam: no config.yaml block and no host env file is not_configured, so no value."""
    store = HandlerSecretStoreSettings(onex_home=tmp_path, home=tmp_path)
    secret = asyncio.run(
        store.read(RUNTIME.secret_store_folder, RUNTIME.onboarding.key_env)
    )
    assert secret is None
