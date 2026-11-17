# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: the standing-rulings register reading is a declared systemd unit (OMN-24944).

The register's rows read over ssh, the ledger bus and the repository clones, which only a pool host holds, so the
reading runs as an hourly timer installed through the host installer. The test checks the unit against the tick
module it runs (the seam) and renders it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_ruling_register_effect.overlay import load_route
from omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register import (
    read_register,
)
from omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register_tick import (
    main as tick_main,
)
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_host_installer_registry_units import (
    test_registry_units_systemd as base,
)
from tests.unit.nodes.node_ruling_register_effect.test_ruling_register_effect import (
    cli_env,
    cli_row,
    register_environ,
)

UNIT = "ruling-register-systemd"
MODULE = "omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register_tick"
ENTRY = (
    "src/omnimarket/nodes/node_ruling_register_effect/runtime_ruling_register_tick.py"
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        f"{base.HANDLER}.interpreter_present", lambda path: path == base.LINUX_PYTHON
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )


def _checkout(tmp_path: Path) -> tuple[Path, str]:
    """A clean branch checkout that carries the unit's entry file, and its revision."""
    path = tmp_path / "branch"
    base.make_checkout(path)
    (path / ENTRY).parent.mkdir(parents=True, exist_ok=True)
    (path / ENTRY).write_text("print('register')\n", encoding="utf-8")
    base._git(["git", "-C", str(path), "add", "-A"], check=True)
    commit = [
        "git",
        "-C",
        str(path),
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
    ]
    base._git([*commit, "commit", "-q", "-m", "y"], check=True)
    revision = base._git(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return path, revision


def test_the_unit_is_declared_with_an_hourly_timer_the_probe_and_the_ledger_reading_environment() -> (
    None
):
    unit = load_units()[UNIT]
    assert unit["platform"] == "systemd"
    assert unit["label"] == "onex-ruling-register"
    assert unit["schedule"]["start_interval"] == 3600
    assert unit["module"] == MODULE
    assert unit["entry_file"] == ENTRY
    assert unit["probe_argv"] == ["--probe"]
    # The rows that read the ledger over the bus find the lane declaration table and the lane under these.
    assert set(unit["environment"]) >= {
        "ONEX_SKILL_OVERLAY_ROOTS",
        "LAB_WORK_HOST_NAME",
        "OMNIBASE_PATH",
        "ONEX_WORKSPACE_CONFIG_ROOT",
        "ONEX_PR_WATCHER_STATE",
        "ONEX_RULING_REGISTER_JOURNAL",
    }
    assert "LAB_WORK_HOST_NAME" in unit["environment_path_overrides"]
    node_dir = base.ROOT / "src/omnimarket/nodes/node_host_installer_registry_units"
    assert (node_dir / str(unit["template"])).is_file()
    assert (node_dir / str(unit["timer_template"])).is_file()
    assert (base.ROOT / ENTRY).is_file()


def test_dry_run_renders_an_hourly_timer_that_runs_the_tick_under_the_host_name(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(path),
            expected_revision=revision,
            # The install names the host it runs on: LAB_WORK_HOST_NAME has no default, as in the store units.
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
                "LAB_WORK_HOST_NAME": "h053",
            },
        )
    )
    assert result.status == "success", result.stderr
    exec_line = next(
        line for line in result.rendered.splitlines() if line.startswith("ExecStart=")
    )
    assert f'"-m" "{MODULE}"' in exec_line
    assert f'"--project" "{path}"' in exec_line
    assert "OnUnitActiveSec=3600" in result.rendered
    assert '"LAB_WORK_HOST_NAME=h053"' in result.rendered
    assert f'"OMNIBASE_PATH={tmp_path}/omni"' in result.rendered
    assert (
        f'"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/workspace-config"' in result.rendered
    )
    assert (
        f'"ONEX_RULING_REGISTER_JOURNAL={tmp_path}/omni/.onex_state/ruling-register/journal.jsonl"'
        in result.rendered
    )
    assert "__" not in result.rendered.replace("__init__", "")


def _rendered_dry_run(tmp_path: Path) -> tuple[str, Path]:
    """The dry run's rendered service and timer text, and the home the install named."""
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays"),
                "LAB_WORK_HOST_NAME": "h053",
            },
        )
    )
    assert result.status == "success", result.stderr
    return result.rendered, tmp_path / "home"


def test_the_rendered_unit_carries_the_whole_register_environment_the_limit_and_run_at_load(
    tmp_path: Path,
) -> None:
    rendered, home = _rendered_dry_run(tmp_path)
    overlays = tmp_path / "overlays" / "overlays"
    for assignment in (
        f"ONEX_PR_WATCHER_STATE={home}/.local/state/onex/pr-watcher-readonly/state.json",
        f"ONEX_STATE_DIR={tmp_path}/omni/.onex_state",
        f"ONEX_SKILL_OVERLAY_ROOTS={overlays}",
        f"ONEX_RULING_REGISTER_JOURNAL={tmp_path}/omni/.onex_state/ruling-register/journal.jsonl",
        "LAB_WORK_HOST_NAME=h053",
        f"OMNIBASE_PATH={tmp_path}/omni",
        f"ONEX_WORKSPACE_CONFIG_ROOT={tmp_path}/workspace-config",
    ):
        assert f'"{assignment}"' in rendered, assignment
    # The 900 s run limit (the register's 8 ledger-reader rows read one after another, 8 x 90 s = 720 s at the longest)
    # and run-at-load.
    assert "TimeoutStartSec=900" in rendered
    assert (
        "OnActiveSec=1s" in rendered
    )  # run at load: the first reading follows the install by a second
    assert "OnUnitActiveSec=3600" in rendered
    assert "UnsetEnvironment=PYTHONPATH" in rendered


def test_an_install_runs_the_probe_with_the_rendered_environment_before_it_loads_the_unit(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    run = base.Recorder()
    result = HandlerRegistryUnits(run).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    probe = next(call for call in run.calls if "--probe" in call)
    assert probe[0] == "env"
    assert MODULE in probe
    assert "LAB_WORK_HOST_NAME=h051" in probe
    # The probe is the module's last argument: it reads nothing and writes nothing.
    assert probe[-1] == "--probe"
    first_systemctl = next(call for call in run.calls if call[0] == "systemctl")
    assert run.calls.index(probe) < run.calls.index(first_systemctl)


def test_the_rendered_state_dir_plus_last_run_json_is_the_declared_evidence_file(
    tmp_path: Path,
) -> None:
    rendered, home = _rendered_dry_run(tmp_path)
    exec_line = next(
        line for line in rendered.splitlines() if line.startswith("ExecStart=")
    )
    words = [word.strip('"') for word in exec_line.removeprefix("ExecStart=").split()]
    state_dir = words[words.index("--state-dir") + 1]
    evidence_file = load_units()[UNIT]["evidence_file"]
    # The tick writes <--state-dir>/last-run.json and the unit's heartbeat reads <home>/<evidence_file>.
    assert Path(state_dir) / "last-run.json" == home / str(evidence_file)
    assert Path(str(evidence_file)).name == "last-run.json"


def test_an_install_that_names_no_host_is_refused_and_never_renders_an_unfilled_host_name(
    tmp_path: Path,
) -> None:
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="dry-run",
            project_path=str(path),
            expected_revision=revision,
            path_overrides={
                "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays" / "overlays")
            },
        )
    )
    assert result.status == "failure"
    assert "LAB_WORK_HOST_NAME" in result.stderr
    assert "{host_name}" in result.stderr
    assert "{host_name}" not in result.rendered


def test_the_unit_names_the_journal_the_route_overlay_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read from the deployment's route overlay when the runner holds it, from the shipped example otherwise.

    If the unit set another name the journal would silently stay in memory (the sink is built from the route's name).
    """
    environ, _deployed = register_environ(tmp_path, monkeypatch)
    journal_env = load_route(environ).journal_env
    assert journal_env == "ONEX_RULING_REGISTER_JOURNAL"
    assert journal_env in load_units()[UNIT]["environment"]


def test_failing_control_a_route_overlay_that_names_another_journal_variable_is_not_the_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = yaml.safe_load(
        (
            base.ROOT
            / "src/omnimarket/nodes/node_ruling_register_effect/overlay.example.yaml"
        ).read_text(encoding="utf-8")
    )
    example["journal_env"] = "SOME_OTHER_JOURNAL"
    install_node_overlay(monkeypatch, tmp_path, "node_ruling_register_effect", example)
    journal_env = load_route({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}).journal_env
    assert journal_env not in load_units()[UNIT]["environment"]


def test_the_installer_creates_the_directory_the_journal_and_the_logs_live_in(
    tmp_path: Path,
) -> None:
    """``<registry_root>/.onex_state/ruling-register``: the unit's logs open there before the tick can run."""
    path, revision = _checkout(tmp_path)
    result = HandlerRegistryUnits(base.Recorder()).handle(
        base.request(
            tmp_path,
            unit=UNIT,
            mode="install",
            project_path=str(path),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    journal = Path(
        load_units()[UNIT]["environment"]["ONEX_RULING_REGISTER_JOURNAL"].replace(
            "{registry_root}", str(tmp_path / "omni")
        )
    )
    assert journal.parent == tmp_path / "omni" / str(
        load_units()[UNIT]["state_directory"]
    )
    assert journal.parent.is_dir()


def test_the_ac4_check_reads_the_installed_status_on_h201_and_asserts_the_head_and_the_age() -> (
    None
):
    contract = yaml.safe_load(
        (base.ROOT / "contracts/OMN-24944.yaml").read_text(encoding="utf-8")
    )
    entry = next(item for item in contract["dod_evidence"] if "AC4" in item["binds_ac"])
    assert entry["execution_scope"] == "local_done_gate"
    command = "\n".join(str(check["check_value"]) for check in entry["checks"])
    # Rerunning the unit tests passes whether or not the unit is installed; the check reads the install itself.
    assert "-m pytest" not in command
    assert "uv run" not in command
    assert "onex-host-install-units ruling-register-systemd --status" in command
    assert "h201" in command
    # The head: the installer's recorded revision is compared with the checked-out head.
    assert "git rev-parse HEAD" in command
    assert "installed revision" in command
    # The age: last-run.json younger than 7200 s (two intervals of the 3600 s timer).
    assert "7200" in command
    assert "s old" in command
    assert load_units()[UNIT]["schedule"]["start_interval"] * 2 == 7200


# The four h201 rows of the deployed register (the work-ledger-free ones), argv verbatim; their commands are docker, curl,
# python3, grep, date and mktemp, all resolved through the unit's PATH.
H201_ROWS: dict[str, tuple[str, dict[str, object]]] = {
    "one-runtime-no-foreign-containers": (
        r"""for c in $(docker ps --filter label=com.docker.compose.project=omnibase-infra --format '{{.Names}}'); do
  img=$(docker inspect -f '{{.Image}}' "$c") || exit 4
  svc=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$c") || exit 4
  tags=$(docker image inspect -f '{{join .RepoTags " "}}' "$img") || exit 4
  case " $tags" in
    " omnibase-infra-"*) [ "$svc" = omninode-runtime ] || echo "$svc" ;;
  esac
done
""",
        {"kind": "stdout_empty"},
    ),
    "no-onex-api-container-on-dev": (
        r"""docker ps --filter label=com.docker.compose.project=omnibase-infra --filter name=onex-api --format '{{.Names}}'""",
        {"kind": "stdout_empty"},
    ),
    "boot-wires-only-attached-contracts": (
        r"""log=$(mktemp) || exit 4
trap 'rm -f "$log"' EXIT
docker logs omninode-runtime >"$log" 2>&1 || exit 4
start=$(docker inspect -f '{{.State.StartedAt}}' omninode-runtime) || exit 4
first=$(grep -m1 -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}' "$log")
[ -n "$first" ] || exit 4
[ "$(date -u -d "$first" +%s)" -le "$(( $(date -u -d "$start" +%s) + 120 ))" ] || exit 4
wired=$(grep -o 'Auto-wiring complete: wired=[0-9]*' "$log" | tail -n 1 | grep -o '[0-9]*$')
[ -n "$wired" ] || exit 4
attached=$(curl -s --max-time 10 localhost:8085/ready | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["details"]["supplemental_readiness"]["contract_attach"]["attached_contracts"]))') || exit 4
echo $((wired - attached))
""",
        {"kind": "int_at_most", "bound": 0},
    ),
    "boot-joins-only-required-contracts": (
        r"""log=$(mktemp) || exit 4
trap 'rm -f "$log"' EXIT
docker logs omninode-runtime >"$log" 2>&1 || exit 4
start=$(docker inspect -f '{{.State.StartedAt}}' omninode-runtime) || exit 4
first=$(grep -m1 -oE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]{8}' "$log")
[ -n "$first" ] || exit 4
[ "$(date -u -d "$first" +%s)" -le "$(( $(date -u -d "$start" +%s) + 120 ))" ] || exit 4
groups=$(grep -oE 'joining group [^ ]+' "$log" | sort -u | wc -l)
[ "$groups" -gt 0 ] || exit 4
required=$(curl -s --max-time 10 localhost:8085/ready | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["details"]["supplemental_readiness"]["contract_attach"]["required_contracts"]))') || exit 4
echo $((groups - required))
""",
        {"kind": "int_at_most", "bound": 0},
    ),
}

# ---- review of head dd0f23ff930f: the register's own checks, run -------------------------------------------------


def _unit_environment(rendered: str) -> dict[str, str]:
    """The ``Environment=`` assignments of a rendered unit, as the service manager would set them."""
    env: dict[str, str] = {}
    for line in rendered.splitlines():
        if line.startswith("Environment="):
            for assignment in shlex.split(line.removeprefix("Environment=")):
                key, _, value = assignment.partition("=")
                env[key] = value
    return env


def _stub_docker_and_curl(directory: Path) -> None:
    """The two commands of an h201 row that live outside coreutils, answering as a healthy dev runtime would."""
    directory.mkdir(parents=True, exist_ok=True)
    docker = directory / "docker"
    docker.write_text(
        """#!/bin/sh
case "$1" in
  ps) case "$*" in *name=onex-api*) ;; *) echo omninode-runtime ;; esac ;;
  logs)
    echo "2026-11-17 06:00:05 INFO Auto-wiring complete: wired=3"
    echo "2026-11-17 06:00:06 INFO joining group omninode-a"
    echo "2026-11-17 06:00:07 INFO joining group omninode-b" ;;
  inspect)
    case "$*" in
      *StartedAt*) echo 2026-11-17T06:00:00.123456789Z ;;
      *Labels*) echo omninode-runtime ;;
      *) echo sha256:abc ;;
    esac ;;
  image) echo omnibase-infra-runtime:latest ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    curl = directory / "curl"
    curl.write_text(
        """#!/bin/sh
echo '{"details":{"supplemental_readiness":{"contract_attach":{"attached_contracts":["a","b","c"],"required_contracts":["x","y"]}}}}'
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl.chmod(0o755)


def _h201_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], dict[str, str], list[dict[str, object]]]:
    """The deployed h201 rows installed under an overlay root and the rendered unit's environment.

    Returns the environ the tick runs under, the rendered unit's own environment and the rows. The process takes the
    unit's HOME, USER, LOGNAME, OMNI_HOME and PATH, so a command that is not on the unit's PATH fails its row.
    """
    path, revision = _checkout(tmp_path)
    rendered = (
        HandlerRegistryUnits(base.Recorder())
        .handle(
            base.request(
                tmp_path,
                unit=UNIT,
                mode="dry-run",
                project_path=str(path),
                expected_revision=revision,
                path_overrides={
                    "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays"),
                    "LAB_WORK_HOST_NAME": "h201",
                },
            )
        )
        .rendered
    )
    unit_env = _unit_environment(rendered)
    assert unit_env["LAB_WORK_HOST_NAME"] == "h201"
    unit_bin = tmp_path / "home" / ".local" / "bin"
    assert unit_env["PATH"] == f"{unit_bin}:/usr/local/bin:/usr/bin:/bin"
    _stub_docker_and_curl(unit_bin)
    for key in ("HOME", "USER", "LOGNAME", "OMNI_HOME", "PATH"):
        monkeypatch.setenv(key, unit_env[key])
    rows: list[dict[str, object]] = [
        {
            "row_id": row_id,
            "ruling": "2026-07-01T10:00:00Z",
            "must_be_true": "the dev runtime holds",
            "read": {
                "host": "h201",
                "argv": ["sh", "-c", script],
                "ok_exit_codes": [0],
                "expect": expect,
            },
            "owner": "example-lane",
            "fix": f"fix {row_id}",
        }
        for row_id, (script, expect) in H201_ROWS.items()
    ]
    overlays = tmp_path / "overlays"
    install_node_overlay(
        monkeypatch,
        overlays,
        "node_ruling_register_compute",
        {"max_reading_age_seconds": 600, "rows": rows},
    )
    install_node_overlay(
        monkeypatch,
        overlays,
        "node_ruling_register_effect",
        {
            "kind": "command",
            "timeout_seconds": 60,
            "remote_argv_prefix": ["ssh", "-o", "BatchMode=yes", "{target}"],
            "local_hosts": ["local"],
            "host_targets": {},
            "journal_env": "ONEX_RULING_REGISTER_JOURNAL",
        },
    )
    return {**unit_env, "ONEX_SKILL_OVERLAY_ROOTS": str(overlays)}, unit_env, rows


@pytest.mark.skipif(sys.platform != "linux", reason="the h201 rows use GNU date")
def test_the_real_h201_rows_run_under_the_units_path_and_user_and_read_locally_under_the_install_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four h201 rows, their argv verbatim, read through the rendered unit's environment.

    ``--set LAB_WORK_HOST_NAME=h201`` is the install's host name; each row's ``read.host`` must equal it, or the row
    goes over ssh to itself and reads UNREAD. The docker and curl of the rows are stubs placed first on the unit's
    PATH (h201 holds them at /usr/bin/docker and /usr/bin/curl); every other command resolves on /usr/bin:/bin.
    """
    environ, unit_env, rows = _h201_register(tmp_path, monkeypatch)
    judged, sink_errors = read_register(environ)
    assert sink_errors == ()
    assert {row["read"]["host"] for row in rows} == {unit_env["LAB_WORK_HOST_NAME"]}  # type: ignore[index]
    assert {v.row_id: v.verdict.value for v in judged.verdicts} == dict.fromkeys(
        H201_ROWS, "TRUE"
    )
    # The same rows under an environment without the host name (the hand-run CLI in the dev container carries none:
    # no compose file or env template of the dev runtime sets it) cannot read themselves: no target, so UNREAD.
    without_name = {k: v for k, v in environ.items() if k != "LAB_WORK_HOST_NAME"}
    unnamed, _ = read_register(without_name)
    assert {v.verdict.value for v in unnamed.verdicts} == {"UNREAD"}


@pytest.mark.skipif(sys.platform != "linux", reason="the h201 rows use GNU date")
def test_failing_control_a_docker_that_refuses_reads_the_real_h201_rows_unread_never_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environ, _unit_env, _rows = _h201_register(tmp_path, monkeypatch)
    docker = tmp_path / "home" / ".local" / "bin" / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    judged, _ = read_register(environ)
    verdicts = {v.row_id: v.verdict.value for v in judged.verdicts}
    # A row that cannot read reads UNREAD, never TRUE. One row of the deployed register is an exception that this test
    # records rather than hides: its loop runs over `$(docker ps ...)`, so a docker that fails gives an empty list, no
    # output and exit 0, which the row's stdout_empty expectation judges TRUE. The overlay owner's row to harden.
    assert verdicts.pop("one-runtime-no-foreign-containers") == "TRUE"
    assert set(verdicts.values()) == {"UNREAD"}


# ---- the AC4 and AC6 checks of the contracts, executed ---------------------------------------------------------------


def _check_command(contract_file: str, ac: str) -> str:
    contract = yaml.safe_load(
        (base.ROOT / "contracts" / contract_file).read_text(encoding="utf-8")
    )
    entry = next(item for item in contract["dod_evidence"] if ac in item["binds_ac"])
    assert entry["execution_scope"] == "local_done_gate"
    (check,) = entry["checks"]
    return str(check["check_value"])


def _write_stub(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    stub = directory / name
    stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    stub.chmod(0o755)


class Ac4World:
    """An h201 stand-in: the installer's real status output, the tick's real last-run.json and journal.

    ``ssh`` drops its host argument and runs the rest here, ``onex-host-install-units`` prints the status the real
    handler rendered, and the checkout's HEAD is the revision the install recorded unless a test moves it.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.checkout, self.revision = _checkout(tmp_path)
        self.bin = tmp_path / "bin"
        self.home = tmp_path / "home"
        self.evidence = self.home / str(load_units()[UNIT]["evidence_file"])
        self.journal = (
            tmp_path / "omni" / ".onex_state" / "ruling-register" / "journal.jsonl"
        )
        self.status = ""
        result = HandlerRegistryUnits(base.Recorder()).handle(
            base.request(
                tmp_path,
                unit=UNIT,
                mode="install",
                project_path=str(self.checkout),
                expected_revision=self.revision,
            )
        )
        assert result.status == "success", result.stderr
        _write_stub(self.bin, "ssh", 'shift\nexec "$@"\n')

    def tick(self, rows: list[dict[str, object]], *, journal: bool = True) -> None:
        """One real tick under ``rows``; its last-run.json lands where the installer's status reads the heartbeat."""
        overlay_root = self.tmp_path / "register"
        cli_env(overlay_root, self.monkeypatch, rows)
        if journal:
            self.monkeypatch.setenv("ONEX_RULING_REGISTER_JOURNAL", str(self.journal))
        else:
            self.monkeypatch.delenv("ONEX_RULING_REGISTER_JOURNAL", raising=False)
        assert tick_main(["--state-dir", str(self.evidence.parent)]) == 0

    def render_status(self) -> str:
        """The installer's own ``--status`` output, read after the tick and any age change."""
        result = HandlerRegistryUnits(base.Recorder()).handle(
            base.request(self.tmp_path, unit=UNIT, mode="status")
        )
        self.status = result.stdout
        status_file = self.tmp_path / "status.txt"
        status_file.write_text(self.status, encoding="utf-8")
        _write_stub(
            self.bin,
            "onex-host-install-units",
            f'[ "$*" = "ruling-register-systemd --status" ] || exit 2\ncat "{status_file}"\n',
        )
        return self.status

    def run_check(self) -> subprocess.CompletedProcess[str]:
        command = _check_command("OMN-24944.yaml", "AC4")
        return subprocess.run(
            shlex.split(command),
            cwd=self.checkout,
            env=scrub_git_location_env(
                {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}"}
            ),
            capture_output=True,
            text=True,
            check=False,
        )


def _row(row_id: str, code: str) -> dict[str, object]:
    return cli_row(row_id, code, {"kind": "exit_zero"})


READS = _row("row-reads", "pass")
UNREADABLE = _row("row-unread", "import sys; sys.exit(9)")


def test_the_status_output_the_check_pins_its_patterns_to_is_the_installers_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick([READS, UNREADABLE])
    status = world.render_status()
    lines = status.splitlines()
    assert lines[0] == "LOADED onex-ruling-register.timer"
    # The first "N s old" line names last-run.json by its full path under the home the install named.
    assert re.fullmatch(rf"{re.escape(str(world.evidence))}: \d+ s old", lines[1])
    assert lines[2] == f"installed revision {world.revision} of {world.checkout}"
    record = json.loads(world.evidence.read_text(encoding="utf-8"))
    assert record["counts"] == {"TRUE": 1, "FALSE": 0, "UNREAD": 1}
    assert record["journal"] == str(world.journal)
    # The record's key order and spacing are what the check's sed patterns read.
    text = world.evidence.read_text(encoding="utf-8")
    assert '"rows": 2' in text
    assert '"UNREAD": 1' in text
    assert f'"journal": "{world.journal}"' in text


def test_the_ac4_check_passes_with_the_head_installed_a_fresh_record_some_rows_read_and_a_journal_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick([READS, UNREADABLE])
    world.render_status()
    result = world.run_check()
    assert result.returncode == 0, result.stdout + result.stderr
    # The check prints the status and last-run.json it judged, so the PR can record them.
    assert f"installed revision {world.revision} of " in result.stdout
    assert '"UNREAD": 1' in result.stdout


def test_the_ac4_check_fails_when_every_row_reads_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick([UNREADABLE, _row("row-unread-2", "import sys; sys.exit(9)")])
    world.render_status()
    assert (
        json.loads(world.evidence.read_text(encoding="utf-8"))["counts"]["UNREAD"] == 2
    )
    assert world.run_check().returncode != 0


def test_the_ac4_check_fails_when_the_journal_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(
        [READS, UNREADABLE], journal=False
    )  # the route's journal variable is unset: events stay in memory
    world.render_status()
    assert not world.journal.exists()
    assert world.run_check().returncode != 0


def test_the_ac4_check_fails_when_the_journal_has_no_line_or_only_an_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick([READS, UNREADABLE])
    world.render_status()
    assert world.run_check().returncode == 0
    old = time.time() - 3 * 3600
    os.utime(world.journal, (old, old))
    assert world.run_check().returncode != 0
    world.journal.write_text("", encoding="utf-8")
    assert world.run_check().returncode != 0


def test_the_ac4_check_fails_for_another_revision_a_stale_record_or_another_first_age_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick([READS, UNREADABLE])
    world.render_status()
    assert world.run_check().returncode == 0
    # A later commit: the checked-out head is no longer the installed revision.
    base._git(
        [
            "git",
            "-C",
            str(world.checkout),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "later",
        ],
        check=True,
    )
    assert world.run_check().returncode != 0
    base._git(
        ["git", "-C", str(world.checkout), "reset", "-q", "--hard", world.revision],
        check=True,
    )
    assert world.run_check().returncode == 0
    # A heartbeat older than two intervals.
    old = time.time() - 3 * 3600
    os.utime(world.evidence, (old, old))
    world.render_status()
    assert "NOT RUNNING" in world.status
    assert world.run_check().returncode != 0
    os.utime(world.evidence, None)
    world.render_status()
    assert world.run_check().returncode == 0
    # The first "N s old" line is another file than last-run.json.
    _write_stub(
        world.bin,
        "onex-host-install-units",
        f'printf "%s\\n" "{world.tmp_path}/other.json: 5 s old" "installed revision {world.revision} of x"\n',
    )
    assert world.run_check().returncode != 0


def test_the_ac6_check_of_the_earlier_round_fails_without_the_lab_comment_for_the_head(
    tmp_path: Path,
) -> None:
    command = _check_command("OMN-25207.yaml", "AC6")
    head = base._git(
        ["git", "rev-parse", "HEAD"],
        cwd=base.ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    bin_dir = tmp_path / "bin"

    def run(pr_view_output: str) -> int:
        (tmp_path / "view.txt").write_text(pr_view_output, encoding="utf-8")
        _write_stub(bin_dir, "onex", f'cat "{tmp_path}/view.txt"\n')
        return subprocess.run(
            shlex.split(command),
            cwd=base.ROOT,
            env=scrub_git_location_env(
                {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            ),
            capture_output=True,
            text=True,
            check=False,
        ).returncode

    bare_head = f'{{"head_sha":"{head}","comments":[]}}\n'
    other_head = f'Lab: head={"0" * 40} host=h053\n{{"head_sha":"{head}"}}\n'
    with_lab = f'{{"head_sha":"{head}"}}\nLab: head={head} host=h053 exit=0\n'
    # The old check (grep for the sha alone) passes on a PR with no Lab comment; the new one does not.
    old = subprocess.run(
        ["sh", "-c", 'printf "%s" "$1" | grep -F "$2"', "_", bare_head, head],
        check=False,
    )
    assert old.returncode == 0
    assert run(bare_head) != 0
    assert run(other_head) != 0
    assert run(with_lab) == 0
