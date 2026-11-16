# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review key credit probe is scheduled by a declared launchd unit of this repository (OMN-24656).

The probe (``onex models review-gateway-key-probe``) reads each review key bound to the Mac with one token and
appends a ledger row when a key's credit state flips. Its schedule had no home outside the frozen internal
repository. It is now the host installer's ``review-key-credit-probe`` registry unit, shaped like the Claude
token pool rotation tick; its settings (tenant, model, host) are the node's overlay, never the unit's. These
tests read the declaration, the unit it renders, and one tick of the module the unit runs.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_claude_review_gateway_effect import (
    runtime_review_key_credit_probe_tick as tick,
)
from omnimarket.nodes.node_claude_review_gateway_effect.models.model_review_key_probe_overlay import (
    ModelReviewKeyProbeOverlay,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    Recorder,
    make_checkout,
    request,
)

pytestmark = pytest.mark.unit

UNIT = "review-key-credit-probe"
LABEL = "ai.omninode.review-key-credit-probe"
NODE = "node_claude_review_gateway_effect"
# The probe reads its own overlay id, never the gateway's (whose overlay carries other sections).
PROBE_OVERLAY_ID = "review_key_credit_probe"
HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
BREW_PYTHON = "/opt/homebrew/bin/python3.13"
ROOT = Path(__file__).resolve().parents[4]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / NODE


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{HANDLER}.interpreter_present", lambda path: path == BREW_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


# What the scheduled command reads beyond the ledger path: the lab host table that names this machine's host row, and
# the ledger writer command it appends a credit flip with. Neither has a default in the command, so an unset one
# refuses every tick (the ledger writer's refusal comes after the keys are read, so each tick spends a token).
LAB_HOSTS_VARIABLE = "ONEX_LAB_RUN_HOSTS"
LEDGER_COMMAND_VARIABLE = "ONEX_LEDGER_COMMAND"


def _ledger_variable() -> str:
    """The environment variable the unit sets from the installing request's ledger path."""
    unit = load_units()[UNIT]
    (variable,) = unit["environment_inputs"]
    assert variable in unit["required_environment_inputs"]
    return str(variable)


def _rendered_environment(tmp_path: Path) -> dict[str, str]:
    """The environment the rendered unit carries, read from the plist the installer renders for a dry run.

    The two variables the installer cannot know (the lab host table, an overlay root) are the request's path overrides,
    as an operator supplies them at install; the table names ``host-1`` so the overlay's host row resolves.
    """
    unit = load_units()[UNIT]
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    table = tmp_path / "lab_hosts.yaml"
    table.write_text(yaml.safe_dump({"hosts": [{"name": "host-1"}]}), encoding="utf-8")
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(branch),
            expected_revision=revision,
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
                LAB_HOSTS_VARIABLE: str(table),
            },
            ledger_path=str(tmp_path / "ledger.md"),
        )
    )
    assert result.status == "success", result.stderr
    loaded = plistlib.loads(result.rendered.encode("utf-8"))
    environment: dict[str, str] = loaded["EnvironmentVariables"]
    return environment


# --- the schedule is declared -----------------------------------------------------------------


def test_the_schedule_is_declared_as_a_launchd_tick_with_a_bounded_run() -> None:
    unit = load_units()[UNIT]
    assert unit["platform"] == "launchd"
    assert unit["kind"] == "registry_module_tick"
    assert unit["label"] == LABEL
    assert unit["schedule"]["start_interval"] > 0
    # A hung probe ends before the next tick is due.
    assert 0 < unit["run_timeout_s"] < unit["schedule"]["start_interval"]
    assert (ROOT / unit["entry_file"]).is_file()
    assert unit["module"] == unit["entry_file"].removeprefix("src/").removesuffix(
        ".py"
    ).replace("/", ".")
    assert unit["module"].startswith(f"omnimarket.nodes.{NODE}.")
    assert unit["probe_argv"] == ["--probe"]
    overrides = unit["environment_path_overrides"]
    assert overrides == [
        "ONEX_SKILL_OVERLAY_ROOTS",
        LAB_HOSTS_VARIABLE,
        LEDGER_COMMAND_VARIABLE,
    ]


def test_the_unit_holds_none_of_the_nodes_settings_and_no_credential() -> None:
    text = json.dumps(load_units()[UNIT])
    for setting in ("tenant", "claude-", "api_key", "secret", "password"):
        assert setting not in text.lower()


def test_the_schedule_matches_the_pool_rotation_tick_exemplar() -> None:
    units = load_units()
    ours, exemplar = units[UNIT], units["claude-pool-rotation-tick"]
    for key in (
        "platform",
        "kind",
        "template",
        "registry_project",
        "tick_no_sync",
        "interpreter_candidates",
        "project_path_override",
    ):
        assert ours[key] == exemplar[key], key


def test_the_rendered_unit_runs_the_tick_module_on_the_declared_interval(
    tmp_path: Path,
) -> None:
    unit = load_units()[UNIT]
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=unit["entry_file"])
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(branch),
            expected_revision=revision,
            path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")},
            ledger_path=str(tmp_path / "ledger.md"),
        )
    )
    assert result.status == "success", result.stderr
    assert f"<string>{LABEL}</string>" in result.rendered
    assert f"<string>{unit['module']}</string>" in result.rendered
    assert f"<integer>{unit['schedule']['start_interval']}</integer>" in result.rendered
    assert "ONEX_SKILL_OVERLAY_ROOTS" in result.rendered
    assert _ledger_variable() in result.rendered
    assert str(tmp_path / "ledger.md") in result.rendered


def test_the_rendered_unit_carries_the_lab_host_table_and_the_ledger_command(
    tmp_path: Path,
) -> None:
    environment = _rendered_environment(tmp_path)
    assert environment[LAB_HOSTS_VARIABLE] == str(tmp_path / "lab_hosts.yaml")
    # The ledger writer is the host's onex-ledger command line, declared as the parity receipt unit declares it.
    parity = load_units()["work-ledger-parity-receipt"]["environment"]
    assert (
        load_units()[UNIT]["environment"][LEDGER_COMMAND_VARIABLE]
        == parity[LEDGER_COMMAND_VARIABLE]
    )
    assert environment[LEDGER_COMMAND_VARIABLE] == str(
        tmp_path / "home" / ".local" / "bin" / "onex-ledger"
    )
    assert environment[_ledger_variable()] == str(tmp_path / "ledger.md")


def test_an_install_without_the_lab_host_table_is_refused(tmp_path: Path) -> None:
    unit = load_units()[UNIT]
    assert LAB_HOSTS_VARIABLE in unit["required_environment_inputs"]
    handler = HandlerRegistryUnits(Recorder())
    with pytest.raises(ValueError, match=f"{LAB_HOSTS_VARIABLE} is unset"):
        handler.environment(
            request(
                tmp_path,
                unit=UNIT,
                mode="install",
                ledger_path=str(tmp_path / "ledger.md"),
                path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "o")},
            ),
            unit,
        )
    named = handler.environment(
        request(
            tmp_path,
            unit=UNIT,
            mode="install",
            ledger_path=str(tmp_path / "ledger.md"),
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "o"),
                LAB_HOSTS_VARIABLE: str(tmp_path / "lab_hosts.yaml"),
            },
        ),
        unit,
    )
    assert named[LAB_HOSTS_VARIABLE] == str(tmp_path / "lab_hosts.yaml")


# --- the command the unit schedules -----------------------------------------------------------


def test_the_scheduled_probe_command_exists_with_the_options_the_tick_passes() -> None:
    from omnimarket.cli.cli_secret import models_group

    assert "review-gateway-key-probe" in models_group.commands, (
        "the models group has no review-gateway-key-probe command at this base; "
        "this unit lands after the change that adds it"
    )
    command = models_group.commands["review-gateway-key-probe"]
    options = {
        option: param
        for param in command.params
        for option in getattr(param, "opts", [])
    }
    for flag in ("--tenant", "--model", "--host", "--state-dir", "--ledger"):
        assert flag in options, flag
    # The ledger option reads the variable the unit's environment names for the installing request's ledger.
    assert options["--ledger"].envvar == _ledger_variable()


def _tick_argv(tmp_path: Path, host: str | None) -> list[str]:
    overlay = ModelReviewKeyProbeOverlay(
        tenant_id="tenant-1", model="model-1", host=host
    )
    return tick.probe_argv(overlay, tmp_path / "state")


def _run_real_probe(
    tmp_path: Path, environment: dict[str, str], host: str | None = "host-1"
) -> subprocess.CompletedProcess[str]:
    """The real command, with the argv a tick builds, under only the given environment and an empty key store."""
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        _tick_argv(tmp_path, host),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=tmp_path,
    )


_HOST_ROW_REFUSALS = (
    "no lab host table on this machine",
    "needs this machine's lab host row",
)
_LEDGER_REFUSAL = "ONEX_LEDGER_COMMAND"


def test_the_real_probe_command_gets_past_both_refusals_under_the_rendered_unit_environment(
    tmp_path: Path,
) -> None:
    """The command the unit schedules, run with a tick's argv under the unit's own environment and an empty key store.

    An empty store has no key to read, so the run ends on its own "no key gave a reading" refusal, after the host row
    resolved and the ledger writer was found. Neither variable has a default, so each missing one is its own refusal.
    """
    environment = _rendered_environment(tmp_path)
    done = _run_real_probe(tmp_path, environment)
    for refusal in (*_HOST_ROW_REFUSALS, _LEDGER_REFUSAL):
        assert refusal not in done.stderr, done.stderr
    assert "no key gave a reading" in done.stderr, done.stderr


@pytest.mark.parametrize(
    ("missing", "refusal"),
    [
        (LAB_HOSTS_VARIABLE, "no lab host table on this machine"),
        (LEDGER_COMMAND_VARIABLE, _LEDGER_REFUSAL),
    ],
)
def test_the_real_probe_command_refuses_when_the_unit_lacks_either_variable(
    tmp_path: Path, missing: str, refusal: str
) -> None:
    """Positive control: take either variable out of the rendered environment and the command refuses, so the test above bites."""
    environment = _rendered_environment(tmp_path)
    del environment[missing]
    done = _run_real_probe(tmp_path, environment)
    assert done.returncode == 1
    assert refusal in done.stderr, done.stderr


# --- one tick ---------------------------------------------------------------------------------


def _overlay_root(
    tmp_path: Path, document: dict[str, str], overlay_id: str = PROBE_OVERLAY_ID
) -> Path:
    home = tmp_path / "overlays" / overlay_id
    home.mkdir(parents=True)
    (home / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return tmp_path / "overlays"


class _Runner:
    def __init__(
        self,
        code: int = 0,
        stdout: str = "review-key-probe: 0 flip(s) written\n",
        stderr: str = "",
    ) -> None:
        self.argv: list[str] = []
        self.code = code
        self.stdout = stdout
        self.stderr = stderr

    def __call__(
        self, argv: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        return subprocess.CompletedProcess(argv, self.code, self.stdout, self.stderr)


class _Raises:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.called = False

    def __call__(
        self, argv: list[str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.called = True
        raise self.error


def _configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, str] | None = None,
) -> Path:
    root = _overlay_root(
        tmp_path,
        document or {"tenant_id": "tenant-1", "model": "model-1", "host": "host-1"},
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return tmp_path / "state"


def _heartbeat(state: Path) -> dict[str, object]:
    return json.loads((state / "last-run.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def _value_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_a_tick_runs_the_probe_command_with_the_overlay_and_writes_its_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _configured(tmp_path, monkeypatch)
    runner = _Runner()
    assert (
        tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)], run=runner)
        == 0
    )
    assert runner.argv[1:3] == ["models", "review-gateway-key-probe"]
    for pair in (
        ["--tenant", "tenant-1"],
        ["--model", "model-1"],
        ["--host", "host-1"],
    ):
        assert any(
            runner.argv[i : i + 2] == pair for i in range(len(runner.argv) - 1)
        ), pair
    assert "--ledger" not in runner.argv
    evidence = _heartbeat(state)
    assert evidence["exit_code"] == 0
    assert evidence["result"] == {
        "probe_exit": 0,
        "summary": "review-key-probe: 0 flip(s) written",
    }
    assert "tenant-1" not in json.dumps(evidence)


def test_a_tick_whose_probe_fails_exits_nonzero_and_records_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _configured(
        tmp_path, monkeypatch, {"tenant_id": "tenant-1", "model": "model-1"}
    )
    assert (
        tick.main(
            ["--omni-home", str(tmp_path), "--state-dir", str(state)], run=_Runner(1)
        )
        == 1
    )
    assert _heartbeat(state)["exit_code"] == 1


def test_a_failing_probes_reason_reaches_the_tick_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _configured(tmp_path, monkeypatch)
    runner = _Runner(
        code=1,
        stdout="review-key-probe: key-a no_credit\n",
        stderr="Error: review-gateway-key-probe refused: the ledger is unwritable\n",
    )
    assert tick.main(["--state-dir", str(state)], run=runner) == 1
    seen = capsys.readouterr()
    assert "the ledger is unwritable" in seen.err
    assert "review-key-probe: key-a no_credit" in seen.out
    # The summary is the flip-count line only; a key's name and state are not in it.
    assert _heartbeat(state)["result"] == {"probe_exit": 1, "summary": ""}


def test_a_timed_out_probe_exits_124_and_writes_a_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _configured(tmp_path, monkeypatch)
    runner = _Raises(subprocess.TimeoutExpired(["onex"], tick.PROBE_TIMEOUT_SECONDS))
    assert tick.main(["--state-dir", str(state)], run=runner) == 124
    evidence = _heartbeat(state)
    assert evidence["exit_code"] == 124
    assert evidence["result"] == {"timed_out_after_s": tick.PROBE_TIMEOUT_SECONDS}


def test_a_timed_out_probes_captured_output_reaches_the_tick_streams_before_the_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _configured(tmp_path, monkeypatch)
    hung = subprocess.TimeoutExpired(
        ["onex"],
        tick.PROBE_TIMEOUT_SECONDS,
        output=b"review-key-probe: key-a ok\n",
        stderr=b"Error: waiting on the provider\n",
    )
    assert tick.main(["--state-dir", str(state)], run=_Raises(hung)) == 124
    seen = capsys.readouterr()
    assert "review-key-probe: key-a ok" in seen.out
    assert "waiting on the provider" in seen.err
    # The partial output is written through before the tick's own notice and its heartbeat.
    assert seen.err.index("waiting on the provider") < seen.err.index("TIMED OUT")
    assert _heartbeat(state)["exit_code"] == 124


def test_a_timed_out_probe_with_text_output_or_none_still_exits_124(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _configured(tmp_path, monkeypatch)
    as_text = subprocess.TimeoutExpired(
        ["onex"], 1, output="partial out\n", stderr="partial err\n"
    )
    assert tick.main(["--state-dir", str(state)], run=_Raises(as_text)) == 124
    seen = capsys.readouterr()
    assert "partial out" in seen.out
    assert "partial err" in seen.err
    silent = subprocess.TimeoutExpired(["onex"], 1)
    assert tick.main(["--state-dir", str(state)], run=_Raises(silent)) == 124
    assert "TIMED OUT" in capsys.readouterr().err


def test_a_probe_that_cannot_start_exits_1_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _configured(tmp_path, monkeypatch)
    runner = _Raises(FileNotFoundError("onex is not installed"))
    assert tick.main(["--state-dir", str(state)], run=runner) == 1
    evidence = _heartbeat(state)
    assert evidence["exit_code"] == 1
    assert evidence["result"] == {"probe_did_not_run": "FileNotFoundError"}
    assert "onex is not installed" in capsys.readouterr().err


def test_a_tick_with_no_overlay_runs_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    runner, state = _Runner(), tmp_path / "state"
    assert (
        tick.main(["--omni-home", str(tmp_path), "--state-dir", str(state)], run=runner)
        == 3
    )
    assert runner.argv == []
    assert _heartbeat(state)["exit_code"] == 3


def test_a_tick_with_an_invalid_overlay_exits_3_runs_nothing_and_names_the_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _configured(tmp_path, monkeypatch, {"tenant_id": "tenant-1"})
    runner = _Runner()
    assert tick.main(["--state-dir", str(state)], run=runner) == 3
    assert runner.argv == []
    assert _heartbeat(state)["exit_code"] == 3
    err = capsys.readouterr().err
    # The message names the file's source and the invalid field, never a value.
    assert "model" in err
    assert "tenant-1" not in err


def test_an_overlay_without_a_host_passes_no_host_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _configured(
        tmp_path, monkeypatch, {"tenant_id": "tenant-1", "model": "model-1"}
    )
    runner = _Runner()
    assert tick.main(["--state-dir", str(state)], run=runner) == 0
    assert "--host" not in runner.argv


def test_key_state_goes_to_the_keys_directory_of_the_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _configured(tmp_path, monkeypatch)
    runner = _Runner()
    assert tick.main(["--state-dir", str(state)], run=runner) == 0
    assert _value_after(runner.argv, "--state-dir") == str(state / "keys")


def test_the_probe_timeout_stays_under_the_units_run_timeout() -> None:
    assert 0 < tick.PROBE_TIMEOUT_SECONDS < load_units()[UNIT]["run_timeout_s"]


def test_the_probe_has_its_own_overlay_id_so_the_gateways_sections_do_not_break_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _configured(tmp_path, monkeypatch)
    gateway_overlay = tmp_path / "overlays" / NODE
    gateway_overlay.mkdir(parents=True)
    (gateway_overlay / "overlay.yaml").write_text(
        yaml.safe_dump(
            {"event_bus": {"bootstrap": "bus:1"}, "contract": {"key": "value"}}
        ),
        encoding="utf-8",
    )
    runner = _Runner()
    assert tick.main(["--state-dir", str(state)], run=runner) == 0
    assert _value_after(runner.argv, "--tenant") == "tenant-1"


def test_a_bare_run_without_a_state_dir_refuses_the_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _configured(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    runner = _Runner()
    assert tick.main([], run=runner) == tick.REFUSED
    assert runner.argv == []
    assert "--state-dir" in capsys.readouterr().err
    assert not (tmp_path / "keys").exists()


def test_the_refusal_exit_is_not_argparses_usage_error_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Launchd's last exit status tells a refused pass from a malformed command line."""
    _configured(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as usage:
        tick.main(["--no-such-argument"], run=_Runner())
    assert usage.value.code == 2
    assert tick.REFUSED not in (0, 1, 2, tick.INDETERMINATE, tick.TIMED_OUT)


def test_the_probe_command_resolves_beside_the_running_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _configured(tmp_path, monkeypatch)
    runner = _Runner()
    assert tick.main(["--state-dir", str(state)], run=runner) == 0
    assert runner.argv[0] == str(Path(sys.executable).with_name("onex"))


def test_the_probe_argument_does_a_pass_of_nothing(tmp_path: Path) -> None:
    runner = _Runner()
    assert tick.main(["--probe"], run=runner) == 0
    assert runner.argv == []


def test_the_overlay_refuses_a_missing_tenant_or_model() -> None:
    with pytest.raises(ValidationError):
        ModelReviewKeyProbeOverlay.model_validate({"model": "model-1"})
    with pytest.raises(ValidationError):
        ModelReviewKeyProbeOverlay.model_validate({"tenant_id": "tenant-1"})
    with pytest.raises(ValidationError):
        ModelReviewKeyProbeOverlay.model_validate(
            {"tenant_id": "t", "model": "m", "extra": "x"}
        )


def test_the_overlay_example_loads_through_the_model() -> None:
    document = yaml.safe_load(
        (NODE_DIR / "review_key_probe.overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert ModelReviewKeyProbeOverlay.model_validate(document).model
