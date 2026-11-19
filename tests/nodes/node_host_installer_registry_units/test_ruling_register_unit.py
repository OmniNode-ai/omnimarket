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
from typing import cast

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.models.model_github_gateway_io import ModelPrComment, ModelPrTextResult
from omnimarket.models.model_ruling_register import ModelRulingRow
from omnimarket.models.scm.model_scm_command import EnumScmNode, ModelScmAnswer
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_ruling_register_compute.overlay import (
    load_ruling_register_policy,
)
from omnimarket.nodes.node_ruling_register_effect.handlers.handler_ruling_register_read import (
    DEFAULT_LEDGER_LOCK,
    LEDGER_LOCK_ENV,
    is_ledger_reader_row,
    resolve_ledger_lock,
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
    assert '"$installer" ruling-register-systemd --status' in command
    assert "${RULING_REGISTER_INSTALLER:?" in command
    assert "h201" in command
    # The head: the installer's recorded revision is compared with the checked-out head.
    assert "git rev-parse HEAD" in command
    assert "installed revision" in command
    # The age: last-run.json younger than 7200 s (two intervals of the 3600 s timer).
    assert "7200" in command
    assert "s old" in command
    assert load_units()[UNIT]["schedule"]["start_interval"] * 2 == 7200


# The row as deployed before the docker list failed loud: its loop ran over `$(docker ps ...)`, so a docker that failed
# gave an empty list, no output and exit 0, which its stdout_empty expectation judged TRUE.
DEPLOYED_FOREIGN_CONTAINERS = r"""for c in $(docker ps --filter label=com.docker.compose.project=omnibase-infra --format '{{.Names}}'); do
  img=$(docker inspect -f '{{.Image}}' "$c") || exit 4
  svc=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$c") || exit 4
  tags=$(docker image inspect -f '{{join .RepoTags " "}}' "$img") || exit 4
  case " $tags" in
    " omnibase-infra-"*) [ "$svc" = omninode-runtime ] || echo "$svc" ;;
  esac
done
"""
# The hardened row: the docker list is read first and a docker that fails ends the row with 4 (a refused read, UNREAD).
HARDENED_FOREIGN_CONTAINERS = r"""names=$(docker ps --filter label=com.docker.compose.project=omnibase-infra --format '{{.Names}}') || exit 4
for c in $names; do
  img=$(docker inspect -f '{{.Image}}' "$c") || exit 4
  svc=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$c") || exit 4
  tags=$(docker image inspect -f '{{join .RepoTags " "}}' "$img") || exit 4
  case " $tags" in
    " omnibase-infra-"*) [ "$svc" = omninode-runtime ] || echo "$svc" ;;
  esac
done
"""

# The four h201 rows of the deployed register (the work-ledger-free ones), argv verbatim; their commands are docker, curl,
# python3, grep, date and mktemp, all resolved through the unit's PATH.
H201_ROWS: dict[str, tuple[str, dict[str, object]]] = {
    "one-runtime-no-foreign-containers": (
        HARDENED_FOREIGN_CONTAINERS,
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

H201_ROW_IDS = (
    "one-runtime-no-foreign-containers",
    "no-onex-api-container-on-dev",
    "boot-wires-only-attached-contracts",
    "boot-joins-only-required-contracts",
)
# The eight deployed rows that run the work-ledger reader (each carries its module name in its argv).
LEDGER_ROW_IDS = (
    "new-work-lands-with-a-fable-verdict",
    "dev-holds-ready",
    "merged-means-running",
    "lanes-placed-bottom-up",
    "fable-reviews-run-on-the-mac",
    "holds-have-an-owner-and-an-unexpired-expiry",
    "plan-slice-round-four-needs-a-scope-cut",
    "lab-fill-skips-under-a-fifth",
)

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
    assert {cast("dict[str, object]", row["read"])["host"] for row in rows} == {
        unit_env["LAB_WORK_HOST_NAME"]
    }
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
    # A unit whose user cannot run docker reads every h201 row UNREAD, the row that lists the containers included.
    assert verdicts == dict.fromkeys(H201_ROWS, "UNREAD")


def test_failing_control_the_row_as_deployed_before_the_hardening_reads_true_when_docker_fails(
    tmp_path: Path,
) -> None:
    """The control for the test above: the loop over ``$(docker ps ...)`` gave exit 0 and no output on a failing docker."""
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    for script, code in (
        (DEPLOYED_FOREIGN_CONTAINERS, 0),
        (HARDENED_FOREIGN_CONTAINERS, 4),
    ):
        result = subprocess.run(
            ["sh", "-c", script],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode, result.stdout) == (code, ""), script


def _h201_row_mismatches(by_id: dict[str, ModelRulingRow]) -> list[str]:
    """The ids of the h201 rows whose deployed row is not the test's row verbatim (argv, host and expectation).

    No earlier text is allowed: the row deployed before the docker list failed loud reads TRUE when docker fails.
    """
    bad = []
    for row_id, (script, expect) in H201_ROWS.items():
        deployed_row = by_id[row_id]
        if not (
            deployed_row.read.host == "h201"
            and deployed_row.read.argv == ("sh", "-c", script)
            and deployed_row.read.expect.kind == expect["kind"]
        ):
            bad.append(row_id)
    return bad


def test_the_h201_rows_of_the_test_match_the_deployed_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each H201_ROWS entry is the deployed row verbatim (argv, host and expectation), with no earlier text allowed: a
    deployed overlay that still carries the docker row as it was before the docker list failed loud fails it. A hosted
    runner holds no deployed overlay and checks only that the entries are well formed."""
    environ, deployed = register_environ(tmp_path, monkeypatch)
    assert set(H201_ROWS) == set(H201_ROW_IDS)
    # The docker list row is the hardened one wherever the test runs (a docker that fails ends it with 4, UNREAD).
    assert (
        H201_ROWS["one-runtime-no-foreign-containers"][0] == HARDENED_FOREIGN_CONTAINERS
    )
    assert "|| exit 4" in HARDENED_FOREIGN_CONTAINERS.splitlines()[0]
    if not deployed:
        assert all(script.strip() for script, _ in H201_ROWS.values())
        return
    by_id = {r.row_id: r for r in load_ruling_register_policy(environ).rows}
    assert _h201_row_mismatches(by_id) == []
    # The eight ledger rows the AC4 check names are the deployed rows that run the ledger reader.
    assert {r.row_id for r in by_id.values() if is_ledger_reader_row(r)} == set(
        LEDGER_ROW_IDS
    )


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
        self.installer = tmp_path / "installer-venv" / "bin" / "onex-host-install-units"
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
        _write_stub(
            self.bin,
            "ssh",
            "shift\n"
            '[ -z "${AC4_SSH_FAIL:-}" ] || exit 255\n'
            '[ "$1" = id ] && [ -n "${AC4_SSH_USER:-}" ] && { echo "$AC4_SSH_USER"; exit 0; }\n'
            'exec "$@"\n',
        )
        # The shell expression of the checked-out head, evaluated when the stubbed PR view runs.
        self.head_expr = f"$(git -C {self.checkout} rev-parse HEAD)"
        self.container_env = "PATH=/usr/bin:/bin\nHOME=/root\n"
        self.docker_ps_rc = 0
        self.curl_rc = 0
        self.lab_line: str | None = None
        self.write_host_stubs()

    def write_host_stubs(self) -> None:
        """What h201 answers to the check: the dev runtime container's environment, docker and curl on the unit's PATH
        and the PR view's Lab line (by default one for the checked-out head that reports every command with exit 0)."""
        env_file = self.tmp_path / "container-env.txt"
        env_file.write_text(self.container_env, encoding="utf-8")
        _write_stub(
            self.bin,
            "docker",
            f'case "$1" in\n  ps) exit {self.docker_ps_rc} ;;\n  exec) cat "{env_file}" ;;\n  *) exit 2 ;;\nesac\n',
        )
        _write_stub(self.bin, "curl", f"exit {self.curl_rc}\n")
        lab = (
            self.lab_line
            if self.lab_line is not None
            else f"Lab: head={self.head_expr} host=h053 cmd=pytest exit=0"
        )
        _write_stub(
            self.bin,
            "onex",
            '[ "$*" = "scm pr-view OmniNode-ai/omnimarket#6345" ] || exit 2\n'
            'echo \'{"head_sha":"x"}\'\n'
            f'echo "{lab}"\n',
        )

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
        # The installer is named by its absolute path on h201; it is not on the PATH ssh gives a command there.
        _write_stub(
            self.installer.parent,
            self.installer.name,
            f'[ "$*" = "ruling-register-systemd --status" ] || exit 2\ncat "{status_file}"\n',
        )
        return self.status

    def run_check(
        self, *, override_path: bool = True, **env: str
    ) -> subprocess.CompletedProcess[str]:
        command = _check_command("OMN-24944.yaml", "AC4")
        unit_path = (
            # The unit's PATH on h201, with the stubs standing for /usr/bin/docker and /usr/bin/curl.
            {"RULING_REGISTER_UNIT_PATH": f"{self.bin}:/usr/bin:/bin"}
            if override_path
            else {}
        )
        return subprocess.run(
            shlex.split(command),
            cwd=self.checkout,
            env=scrub_git_location_env(
                {
                    **{
                        k: v
                        for k, v in os.environ.items()
                        if k != "RULING_REGISTER_UNIT_PATH"
                    },
                    "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "RULING_REGISTER_INSTALLER": str(self.installer),
                    **unit_path,
                    **env,
                }
            ),
            capture_output=True,
            text=True,
            check=False,
        )


def _row(row_id: str, code: str) -> dict[str, object]:
    return cli_row(row_id, code, {"kind": "exit_zero"})


READS = _row("row-reads", "pass")
UNREADABLE = _row("row-unread", "import sys; sys.exit(9)")
# The rows the check requires to be read: the eight that run the ledger reader and the four h201 rows.
REQUIRED_IDS = (*LEDGER_ROW_IDS, *H201_ROW_IDS)
REQUIRED_ROWS = [_row(row_id, "pass") for row_id in REQUIRED_IDS]
# A register whose required rows are all read and one other row is not (the check allows that).
PASSING_ROWS = [*REQUIRED_ROWS, UNREADABLE]


def _unread(*row_ids: str) -> list[dict[str, object]]:
    """The register with each named row unreadable and every other required row read, plus the unread extra row."""
    return [
        _row(row_id, "import sys; sys.exit(9)") if row_id in row_ids else row
        for row_id, row in zip(REQUIRED_IDS, REQUIRED_ROWS, strict=True)
    ] + [UNREADABLE]


def test_the_status_output_the_check_pins_its_patterns_to_is_the_installers_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    status = world.render_status()
    lines = status.splitlines()
    assert lines[0] == "LOADED onex-ruling-register.timer"
    # The first "N s old" line names last-run.json by its full path under the home the install named.
    assert re.fullmatch(rf"{re.escape(str(world.evidence))}: \d+ s old", lines[1])
    assert lines[2] == f"installed revision {world.revision} of {world.checkout}"
    record = json.loads(world.evidence.read_text(encoding="utf-8"))
    assert record["counts"] == {"TRUE": 12, "FALSE": 0, "UNREAD": 1}
    assert record["journal"] == str(world.journal)
    # The record's key order and spacing are what the check's sed patterns read.
    text = world.evidence.read_text(encoding="utf-8")
    assert '"rows": 13' in text
    assert '"UNREAD": 1' in text
    assert f'"journal": "{world.journal}"' in text


def test_the_ac4_check_passes_with_the_head_installed_a_fresh_record_the_required_rows_read_and_a_journal_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    world.render_status()
    result = world.run_check()
    assert result.returncode == 0, result.stdout + result.stderr
    # The check prints the status and last-run.json it judged, so the PR can record them.
    assert f"installed revision {world.revision} of " in result.stdout
    assert '"UNREAD": 1' in result.stdout
    # The Lab line is the PR comment's, recorded from this output; the check does not read it.
    assert "Lab: head=" not in result.stdout


def test_the_ac4_check_fails_when_one_row_is_read_and_the_rest_are_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unread < rows`` passed on one read row: every ledger-reader row and every h201 row must be read."""
    world = Ac4World(tmp_path, monkeypatch)
    world.tick([READS, UNREADABLE, _row("row-unread-2", "import sys; sys.exit(9)")])
    world.render_status()
    counts = json.loads(world.evidence.read_text(encoding="utf-8"))["counts"]
    assert counts["UNREAD"] < 3  # the old check's whole condition holds
    assert world.run_check().returncode != 0


@pytest.mark.parametrize("row_id", REQUIRED_IDS)
def test_the_ac4_check_fails_when_any_one_required_row_reads_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row_id: str
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    world.render_status()
    assert world.run_check().returncode == 0
    world.tick(_unread(row_id))
    world.render_status()
    result = world.run_check()
    assert result.returncode != 0
    assert f"row {row_id} is not read" in result.stdout


def test_the_ac4_check_fails_when_the_dev_container_carries_the_host_name_or_docker_or_curl_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    world.render_status()
    assert world.run_check().returncode == 0
    # The h201 rows would run inside the container and read themselves, so the container must carry no host name.
    world.container_env = "PATH=/usr/bin:/bin\nLAB_WORK_HOST_NAME=h201\n"
    world.write_host_stubs()
    result = world.run_check()
    assert result.returncode != 0
    assert "carries LAB_WORK_HOST_NAME" in result.stdout
    world.container_env = "PATH=/usr/bin:/bin\n"
    world.docker_ps_rc = 1  # the unit's user cannot run docker
    world.write_host_stubs()
    assert world.run_check().returncode != 0
    world.docker_ps_rc = 0
    world.curl_rc = 127  # curl is not on the unit's PATH
    world.write_host_stubs()
    assert world.run_check().returncode != 0
    world.curl_rc = 0
    world.write_host_stubs()
    assert world.run_check().returncode == 0


def test_the_ac4_check_does_not_read_the_lab_line_it_records_its_output_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check's output is what the Lab line records, so it cannot need a Lab line (with exit=0) to first pass:
    it passes with no Lab comment at all and whatever the comment says. The Lab line of the head is checked by its
    own check (contracts/OMN-25318.yaml, AC8)."""
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    world.render_status()
    for line in (
        "no lab comment here",
        f"Lab: head={'0' * 40} host=h053 exit=0",
        f"Lab: head={world.head_expr} host=h053 cmd=a exit=0 cmd=b exit=1",
    ):
        world.lab_line = line
        world.write_host_stubs()
        assert world.run_check().returncode == 0, line
    assert "pr-view" not in _check_command("OMN-24944.yaml", "AC4")
    assert "Lab: head" not in _check_command("OMN-24944.yaml", "AC4")


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
        PASSING_ROWS, journal=False
    )  # the route's journal variable is unset: events stay in memory
    world.render_status()
    assert not world.journal.exists()
    assert world.run_check().returncode != 0


def test_the_ac4_check_fails_when_the_journal_has_no_line_or_only_an_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
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
    world.tick(PASSING_ROWS)
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
        world.installer.parent,
        world.installer.name,
        f'printf "%s\\n" "{world.tmp_path}/other.json: 5 s old" "installed revision {world.revision} of x"\n',
    )
    assert world.run_check().returncode != 0


def _bus_view(head: str, comments: list[tuple[int, str, str]]) -> str:
    """What ``onex scm pr-view <pr> --source bus`` prints, built through the reader's own answer and result models (the
    gateway's ``pr_text`` read: the PR with its conversation comments). ``comments`` are (id, created_at, body)."""
    result = ModelPrTextResult(
        repo="OmniNode-ai/omnimarket",
        pr_number=6345,
        title="t",
        body="b",
        body_truncated=False,
        state="open",
        is_draft=True,
        merged=False,
        author="<author>",
        head_ref="omn-24944-ruling-register-tick",
        head_sha=head,
        base_ref="dev",
        truncated=False,
        comments=[
            ModelPrComment(
                id=cid,
                author="<author>",
                created_at=at,
                updated_at=at,
                body=body,
                body_truncated=False,
            )
            for cid, at, body in comments
        ],
    )
    answer = ModelScmAnswer(
        verb="pr-view",
        node=EnumScmNode("node_github_repo_gateway_effect"),
        operation="pr_text",
        repo="OmniNode-ai/omnimarket",
        source="bus",
        outcome="completed",
        result=result.model_dump(mode="json"),
    )
    return json.dumps(answer.model_dump(mode="json"), separators=(",", ":")) + "\n"


def _lab_check(contract_file: str, ac: str) -> str:
    contract = yaml.safe_load(
        (base.ROOT / "contracts" / contract_file).read_text(encoding="utf-8")
    )
    entry = next(
        item
        for item in contract["dod_evidence"]
        if ac in item["binds_ac"] and "Lab" in str(item)
    )
    assert entry["execution_scope"] == "local_done_gate"
    (check,) = (c for c in entry["checks"] if "Lab: head=" in str(c["check_value"]))
    return str(check["check_value"])


@pytest.mark.parametrize(
    ("contract_file", "ac"),
    [
        ("OMN-25207.yaml", "AC6"),
        ("OMN-25283.yaml", "AC8"),
        ("OMN-25318.yaml", "AC8"),
        ("OMN-25377.yaml", "AC8"),
    ],
    ids=["round-two", "round-three", "round-four", "round-five"],
)
def test_the_lab_comment_check_reads_the_comments_and_matches_only_the_newest_lab_comment(
    tmp_path: Path, contract_file: str, ac: str
) -> None:
    """The Lab checks read the PR's comments through the gateway (``pr-view --source bus``; the watcher's local answer
    carries none) and judge only the newest comment starting ``Lab: head=``: it must name the head, an exit=0 and no
    failed exit. An older Lab comment for the head does not stand in for a newer one at another head."""
    command = _lab_check(contract_file, ac)
    assert "--source bus" in command
    head = base._git(
        ["git", "rev-parse", "HEAD"],
        cwd=base.ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    other = "0" * 40
    bin_dir = tmp_path / "bin"

    def run(view: str) -> int:
        (tmp_path / "view.txt").write_text(view, encoding="utf-8")
        _write_stub(
            bin_dir,
            "onex",
            '[ "$*" = "scm pr-view OmniNode-ai/omnimarket#6345 --source bus" ] || exit 2\n'
            f'cat "{tmp_path}/view.txt"\n',
        )
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

    old_at, new_at = "2026-11-17T10:00:00Z", "2026-11-17T12:00:00Z"
    good = f"Lab: head={head} host=h053 cmd=pytest exit=0"
    # The watcher's own answer (the committed reader output) carries no comments: unread, never passed.
    assert run(PR_VIEW_FIXTURE.read_text(encoding="utf-8")) != 0
    assert run(_bus_view(head, [])) != 0
    assert run(_bus_view(head, [(1, new_at, "a review, no Lab line")])) != 0
    assert run(_bus_view(head, [(1, new_at, f"Lab: head={other} exit=0")])) != 0
    # Control: the newest Lab comment names the head with exit=0.
    assert (
        run(
            _bus_view(
                head, [(1, old_at, f"Lab: head={other} exit=0"), (2, new_at, good)]
            )
        )
        == 0
    )
    assert run(_bus_view(head, [(2, new_at, good), (3, new_at, "a later review")])) == 0
    # Only the newest Lab comment counts: an older one for the head does not pass a newer one at another head.
    assert (
        run(
            _bus_view(
                head, [(1, old_at, good), (2, new_at, f"Lab: head={other} exit=0")]
            )
        )
        != 0
    )
    assert (
        run(_bus_view(head, [(1, old_at, good), (2, new_at, f"{good} cmd=b exit=1")]))
        != 0
    )
    assert run(_bus_view(head, [(1, new_at, f"Lab: head={head} host=h053 cmd=a")])) != 0


# ---- review of head 438e88419fa9 --------------------------------------------------------------------------------


def test_the_failing_docker_row_the_deployed_overlay_still_carries_is_not_accepted_as_the_deployed_row() -> (
    None
):
    """The match test no longer allows the row as it was before the hardening: a deployed docker row that reads TRUE
    when docker fails is a mismatch, and the hardened row is not."""

    def deployed(docker_row_script: str) -> dict[str, ModelRulingRow]:
        rows: dict[str, ModelRulingRow] = {}
        for row_id, (script, expect) in H201_ROWS.items():
            chosen = (
                docker_row_script
                if row_id == "one-runtime-no-foreign-containers"
                else script
            )
            rows[row_id] = ModelRulingRow.model_validate(
                {
                    "row_id": row_id,
                    "ruling": "2026-07-01T10:00:00Z",
                    "must_be_true": "the dev runtime holds",
                    "read": {
                        "host": "h201",
                        "argv": ["sh", "-c", chosen],
                        "ok_exit_codes": [0],
                        "expect": expect,
                    },
                    "owner": "example-lane",
                    "fix": f"fix {row_id}",
                }
            )
        return rows

    assert _h201_row_mismatches(deployed(HARDENED_FOREIGN_CONTAINERS)) == []
    assert _h201_row_mismatches(deployed(DEPLOYED_FOREIGN_CONTAINERS)) == [
        "one-runtime-no-foreign-containers"
    ]


def test_the_ac4_check_default_path_starts_with_the_unit_users_local_bin_and_the_ssh_user_is_the_units_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no override the check resolves docker and curl on the PATH the rendered unit has: the unit user's
    ``.local/bin`` first (the home of the ssh user), then /usr/local/bin:/usr/bin:/bin. It also fails when the ssh user is
    not the user that owns last-run.json (the unit's user)."""
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    world.render_status()
    unit_bin = world.home / ".local" / "bin"
    _write_stub(
        unit_bin, "docker", "exit 1\n"
    )  # the unit user's docker fails: found first on the default PATH
    _write_stub(unit_bin, "curl", "exit 0\n")
    result = world.run_check(override_path=False, HOME=str(world.home))
    assert result.returncode != 0
    _write_stub(unit_bin, "docker", "exit 0\n")
    assert world.run_check(override_path=False, HOME=str(world.home)).returncode == 0, (
        "the check reads docker from the unit user's .local/bin"
    )
    me = subprocess.run(
        ["id", "-un"], capture_output=True, text=True, check=True
    ).stdout.strip()
    other = world.run_check(
        override_path=False, HOME=str(world.home), AC4_SSH_USER=f"not-{me}"
    )
    assert other.returncode != 0
    assert "not the user of the unit" in other.stdout
    assert (
        world.run_check(
            override_path=False, HOME=str(world.home), AC4_SSH_USER=me
        ).returncode
        == 0
    )


def _proof_check(index: int) -> str:
    contract = yaml.safe_load(
        (base.ROOT / "contracts/OMN-25318.yaml").read_text(encoding="utf-8")
    )
    entry = next(
        item
        for item in contract["dod_evidence"]
        if item["id"] == "dod-omn25318-integration-proof"
    )
    return str(entry["checks"][index]["check_value"])


class ProofWorld:
    """Stand-ins for the serial proof: ``uv`` writes a last-run.json under the state dir the check passes, ``ssh`` runs
    the rest here, ``pgrep`` answers whether h201's unit is mid-tick, ``docker`` answers the container's environment."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.bin = tmp_path / "bin"
        self.record = tmp_path / "record.json"
        self.h201_record = tmp_path / ".local/state/omni/ruling-register/last-run.json"
        self.h201_record.parent.mkdir(parents=True)
        self.set_record(dict.fromkeys(LEDGER_ROW_IDS, "TRUE"))
        self.pgrep_rc = 1
        self.ssh_fail = False
        # The pgrep call (1 = the guard before the tick, 2 = the guard after it) whose ssh exits 255; None fails none.
        self.ssh_fail_pgrep_call: int | None = None
        self.container_env = "PATH=/usr/bin:/bin\n"
        self.write()

    def set_record(self, verdicts: dict[str, str]) -> None:
        text = json.dumps(
            {
                "rows": 16,
                "counts": {"TRUE": 8, "FALSE": 0, "UNREAD": 8},
                "verdicts": verdicts,
            },
            sort_keys=True,
        )
        self.record.write_text(text, encoding="utf-8")
        self.h201_record.write_text(text, encoding="utf-8")

    def write(self) -> None:
        env_file = self.tmp_path / "container-env.txt"
        env_file.write_text(self.container_env, encoding="utf-8")
        _write_stub(
            self.bin,
            "uv",
            f'printf "%s\\n" "ONEX_STATE_DIR=${{ONEX_STATE_DIR:-}}" "LOCK=${{ONEX_RULING_REGISTER_LEDGER_LOCK:-}}" > "{self.tmp_path}/uv-env.txt"\n'
            'while [ "$#" -gt 0 ]; do [ "$1" = --state-dir ] && d=$2; shift; done\n'
            f'cp "{self.record}" "$d/last-run.json"\n'
            'echo "RULING-REGISTER rows=16 TRUE=8 FALSE=0 UNREAD=8"\n',
        )
        counter = self.tmp_path / "pgrep-calls.txt"
        counter.write_text("0", encoding="utf-8")
        fail_at = self.ssh_fail_pgrep_call or 0
        _write_stub(
            self.bin,
            "ssh",
            "shift\n"
            + ("exit 255\n" if self.ssh_fail else "")
            + 'if [ "$1" = pgrep ]; then\n'
            f'  n=$(( $(cat "{counter}") + 1 )); echo "$n" > "{counter}"\n'
            f'  [ "$n" = {fail_at} ] && exit 255\n'
            "fi\n"
            'exec "$@"\n',
        )
        _write_stub(self.bin, "pgrep", f"exit {self.pgrep_rc}\n")
        _write_stub(self.bin, "docker", f'cat "{env_file}"\n')

    def run(
        self, index: int, *, drop: tuple[str, ...] = ()
    ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "OMNI_HOME": str(self.tmp_path),
            "ONEX_STATE_DIR": str(self.tmp_path / "state"),
            "ONEX_RULING_REGISTER_LEDGER_LOCK": str(self.tmp_path / "stray.lock"),
        }
        for name in drop:
            env.pop(name, None)
        return subprocess.run(
            shlex.split(_proof_check(index)),
            cwd=self.tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


def test_the_serial_proof_requires_the_eight_ledger_rows_answered_in_its_own_record(
    tmp_path: Path,
) -> None:
    """A row that timed out reads UNREAD in last-run.json (the runner writes no ``TimeoutExpired`` on stdout), so the
    check reads the eight ids from the record: TRUE or FALSE for each, and a missing id fails too."""
    world = ProofWorld(tmp_path)
    ok = world.run(0)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    world.set_record(
        {**dict.fromkeys(LEDGER_ROW_IDS, "TRUE"), LEDGER_ROW_IDS[2]: "FALSE"}
    )
    assert world.run(0).returncode == 0  # a FALSE reading is an answer
    for row_id in LEDGER_ROW_IDS:
        world.set_record({**dict.fromkeys(LEDGER_ROW_IDS, "TRUE"), row_id: "UNREAD"})
        result = world.run(0)
        assert result.returncode != 0, row_id
        assert f"row {row_id} is not answered" in result.stdout
    world.set_record(dict.fromkeys(LEDGER_ROW_IDS[:7], "TRUE"))  # the eighth is absent
    assert world.run(0).returncode != 0


def test_the_serial_proof_fails_when_h201_is_mid_tick_and_the_container_read_fails_when_ssh_or_docker_fails(
    tmp_path: Path,
) -> None:
    world = ProofWorld(tmp_path)
    assert world.run(0).returncode == 0
    # h201's own unit is running a tick while the proof reads the same query service: the runs overlap.
    world.pgrep_rc = 0
    world.write()
    overlap = world.run(0)
    assert overlap.returncode != 0
    assert "mid-tick" in overlap.stdout
    world.pgrep_rc = 1
    world.write()
    # The container read: control first, then ssh failing, then a container that carries the host name.
    assert world.run(1).returncode == 0
    world.ssh_fail = True
    world.write()
    assert world.run(1).returncode != 0  # grep -c printed 0 here and passed
    world.ssh_fail = False
    world.container_env = "PATH=/usr/bin:/bin\nLAB_WORK_HOST_NAME=h201\n"
    world.write()
    assert world.run(1).returncode != 0


def test_the_title_check_reads_the_title_field_and_not_a_comment(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load(
        (base.ROOT / "contracts/OMN-25318.yaml").read_text(encoding="utf-8")
    )
    entry = next(d for d in contract["dod_evidence"] if d["id"] == "dod-omn25318-ac8")
    command = str(entry["checks"][1]["check_value"])
    bin_dir = tmp_path / "bin"

    def run(view: str) -> int:
        (tmp_path / "view.txt").write_text(view, encoding="utf-8")
        _write_stub(bin_dir, "onex", f'cat "{tmp_path}/view.txt"\n')
        return subprocess.run(
            shlex.split(command),
            cwd=tmp_path,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=False,
        ).returncode

    titled = (
        '{"result":{"title":"OMN-24944: ruling-register tick, systemd unit, host-local reads and more",'
        '"state":"OPEN"}}\n'
    )
    comment_only = (
        '{"result":{"title":"OMN-24944: something else","state":"OPEN",'
        '"comments":["a reviewer wrote: tick, unit, local reads"]}}\n'
    )
    assert run(titled) == 0
    assert run(comment_only) != 0
    # Control: the whole-view grep this replaces passed on the comment.
    old = subprocess.run(
        [
            "sh",
            "-c",
            'printf "%s" "$1" | grep -E "tick, .*unit, .*local reads"',
            "_",
            comment_only,
        ],
        check=False,
    )
    assert old.returncode == 0


# ---- review of head 024cb329a9ab -------------------------------------------------------------------------------------

# The output of ``onex scm pr-view OmniNode-ai/omnimarket#6345`` as the reader printed it on 2026-11-17T16:04Z (the
# author is scrubbed): the watcher's PR record, with the title and no body and no comments.
PR_VIEW_FIXTURE = Path(__file__).parent / "fixtures" / "pr_view_omnimarket_6345.json"


def test_the_ac4_check_names_the_installer_by_its_path_and_never_bare_on_the_ssh_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """onex-host-install-units is not on h201's non-interactive ssh PATH, so the check names it by absolute path; the
    stub stands only at that path (nothing on PATH answers the bare name) and the check passes."""
    command = _check_command("OMN-24944.yaml", "AC4")
    assert not re.search(r"(ssh|on_host)\s+(\S+\s+)?onex-host-install-units", command)
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    world.render_status()
    assert not (world.bin / "onex-host-install-units").exists()
    ok = world.run_check()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    # A bare name is refused before ssh runs it.
    bare = world.run_check(RULING_REGISTER_INSTALLER="onex-host-install-units")
    assert bare.returncode != 0
    assert "absolute path" in bare.stdout
    # With no installer path named the check fails; it never falls back to the bare name.
    unnamed = world.run_check(RULING_REGISTER_INSTALLER="")
    assert unnamed.returncode != 0
    assert "RULING_REGISTER_INSTALLER" in unnamed.stderr


def test_the_ac4_check_fails_loud_when_the_host_alias_does_not_answer_and_reads_h201_locally_on_h201(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alias h201 resolves on neither h053 nor h201 itself: an alias that does not answer fails the check with the
    variable to set, and RULING_REGISTER_HOST=local reads the host the check runs on without ssh."""
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    world.render_status()
    unreachable = world.run_check(AC4_SSH_FAIL="1")
    assert unreachable.returncode != 0
    assert "RULING_REGISTER_HOST" in unreachable.stdout
    local = world.run_check(AC4_SSH_FAIL="1", RULING_REGISTER_HOST="local")
    assert local.returncode == 0, local.stdout + local.stderr


def test_the_ac4_check_fails_when_last_run_json_was_written_before_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = Ac4World(tmp_path, monkeypatch)
    world.tick(PASSING_ROWS)
    world.render_status()
    record = world.journal.parent / f"{UNIT}.installed.json"
    installed = json.loads(record.read_text(encoding="utf-8"))
    assert world.run_check().returncode == 0  # control: the tick ran after the install
    installed["installed_at"] = (
        "2999-01-01T00:00:00Z"  # an install after the tick that wrote the record
    )
    record.write_text(json.dumps(installed, indent=1), encoding="utf-8")
    stale = world.run_check()
    assert stale.returncode != 0
    assert "before the install" in stale.stdout


@pytest.mark.parametrize("contract_file", ["OMN-25318.yaml", "OMN-25377.yaml"])
def test_the_overlap_guard_passes_only_on_pgrep_exit_1_and_fails_when_ssh_fails_on_each_guard(
    tmp_path: Path, contract_file: str
) -> None:
    """``if ssh h201 pgrep`` read ssh exit 255 as not mid-tick; the guard now passes only on pgrep's exit 1 (no
    process), and ssh failing on either guard call, or pgrep failing, fails the proof."""
    contract = yaml.safe_load(
        (base.ROOT / "contracts" / contract_file).read_text(encoding="utf-8")
    )
    proof_id = (
        "dod-omn25318-integration-proof"
        if contract_file == "OMN-25318.yaml"
        else "dod-omn25377-integration-proof"
    )
    entry = next(d for d in contract["dod_evidence"] if d["id"] == proof_id)
    command = str(entry["checks"][0]["check_value"])
    assert "if ssh h201 pgrep" not in command
    world = ProofWorld(tmp_path)

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            shlex.split(command),
            cwd=tmp_path,
            env={
                **os.environ,
                "PATH": f"{world.bin}:{os.environ['PATH']}",
                "OMNI_HOME": str(tmp_path),
            },
            capture_output=True,
            text=True,
            check=False,
        )

    ok = run()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    guards = 2 if contract_file == "OMN-25318.yaml" else 1
    for call in range(1, guards + 1):
        world.ssh_fail_pgrep_call = call
        world.write()
        failed = run()
        assert failed.returncode != 0, call
        assert "did not answer (exit 255)" in failed.stdout, call
    world.ssh_fail_pgrep_call = None
    world.pgrep_rc = 2  # pgrep itself fails: unread, never "not mid-tick"
    world.write()
    assert run().returncode != 0


def test_the_unit_sets_the_ledger_lock_under_its_state_directory_not_the_temp_directory(
    tmp_path: Path,
) -> None:
    """The unit names no lock file: the lock derives from the ONEX_STATE_DIR it renders, which is the installer's state
    directory, so a hand run carrying the same ONEX_STATE_DIR takes the same file."""
    rendered, _home = _rendered_dry_run(tmp_path)
    state_directory = str(load_units()[UNIT]["state_directory"])
    lock = tmp_path / "omni" / state_directory / "ledger-reader.lock"
    assert f"{LEDGER_LOCK_ENV}=" not in rendered
    match = re.search(r'"ONEX_STATE_DIR=([^"]+)"', rendered)
    assert match is not None
    assert resolve_ledger_lock({"ONEX_STATE_DIR": match.group(1)}) == lock
    assert (
        resolve_ledger_lock({"ONEX_STATE_DIR": match.group(1), LEDGER_LOCK_ENV: ""})
        == lock
    )
    # The handler's last-resort default (the temp directory) is not what the unit runs with.
    assert resolve_ledger_lock({}) == DEFAULT_LEDGER_LOCK
    assert str(DEFAULT_LEDGER_LOCK) not in rendered
    assert LEDGER_LOCK_ENV == "ONEX_RULING_REGISTER_LEDGER_LOCK"


def test_the_real_pr_view_output_carries_the_title_field_with_no_space_and_no_comments(
    tmp_path: Path,
) -> None:
    """The fixture is the reader's own output: the title check matches it, and the Lab-comment checks cannot match it
    because pr-view prints no body and no comments (they fail closed until a reader shows the comment)."""
    view = PR_VIEW_FIXTURE.read_text(encoding="utf-8")
    record = json.loads(view)
    assert record["verb"] == "pr-view"
    assert '"title":"' in view
    assert '"title": "' not in view
    assert "comments" not in record["result"]
    assert "Lab: head=" not in view
    contract = yaml.safe_load(
        (base.ROOT / "contracts/OMN-25318.yaml").read_text(encoding="utf-8")
    )
    entry = next(d for d in contract["dod_evidence"] if d["id"] == "dod-omn25318-ac8")
    lab_check, title_check = (str(c["check_value"]) for c in entry["checks"])

    def run(command: str, tmp: Path) -> int:
        bin_dir = tmp / "bin"
        _write_stub(bin_dir, "onex", f'cat "{PR_VIEW_FIXTURE}"\n')
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

    assert run(title_check, tmp_path) == 0
    assert run(lab_check, tmp_path) != 0


def test_the_omn25377_ac1_and_ac2_bind_only_to_local_gate_checks_that_read_h201_and_the_deployed_overlay() -> (
    None
):
    contract = yaml.safe_load(
        (base.ROOT / "contracts/OMN-25377.yaml").read_text(encoding="utf-8")
    )
    for ac, needle in (
        ("AC1", "ruling-register-systemd.installed.json"),
        (
            "AC2",
            "ONEX_RULING_REGISTER_DEPLOYED_ROOTS=/data/omninode/overlays/overlays ",
        ),
    ):
        entries = [d for d in contract["dod_evidence"] if ac in d["binds_ac"]]
        assert entries, ac
        for entry in entries:
            assert entry.get("execution_scope") == "local_done_gate", entry["id"]
            for check in entry["checks"]:
                assert check["check_type"] == "command", entry["id"]
        assert any(
            needle in str(c["check_value"]) for e in entries for c in e["checks"]
        ), ac


@pytest.mark.parametrize(
    ("contract_file", "ac"),
    [
        ("OMN-25318.yaml", "AC5"),
        ("OMN-25377.yaml", "AC3"),
        ("OMN-25318.yaml", "AC4"),
        ("OMN-25318.yaml", "AC6"),
        ("OMN-25377.yaml", "AC2"),
    ],
)
def test_the_deployed_overlay_falsifiers_name_the_local_gate_command(
    contract_file: str, ac: str
) -> None:
    """Plain pytest passes on the example overlay, so a falsifier that names it cannot fail; it names the local Done
    gate command that sets the deployed overlay root."""
    contract = yaml.safe_load(
        (base.ROOT / "contracts" / contract_file).read_text(encoding="utf-8")
    )
    (acceptance,) = (
        a for r in contract["requirements"] for a in r["acceptance"] if a["id"] == ac
    )
    falsifier = acceptance["statement"].split(" -- falsifier: ", 1)[1]
    assert not falsifier.startswith("`uv run pytest")
    assert (
        "ONEX_RULING_REGISTER_DEPLOYED_ROOTS=/data/omninode/overlays/overlays "
        in falsifier
    )


# ---- review of head a92ec8dfc09d -------------------------------------------------------------------------------------

ROUND_CONTRACTS = (
    "OMN-24944.yaml",
    "OMN-25207.yaml",
    "OMN-25283.yaml",
    "OMN-25318.yaml",
    "OMN-25377.yaml",
)


def _acceptance(contract_file: str, ac: str) -> str:
    contract = yaml.safe_load(
        (base.ROOT / "contracts" / contract_file).read_text(encoding="utf-8")
    )
    (acceptance,) = (
        a for r in contract["requirements"] for a in r["acceptance"] if a["id"] == ac
    )
    return str(acceptance["statement"])


@pytest.mark.parametrize("contract_file", ROUND_CONTRACTS)
def test_no_check_of_the_rounds_names_ssh_h201_and_every_host_read_goes_through_on_host(
    contract_file: str,
) -> None:
    """``ssh h201`` resolves on neither the gate host h053 nor h201 itself; a host read names RULING_REGISTER_HOST
    (the gate host's alias of h201, ``local`` on h201) through ``on_host``, which fails loud when the alias does not
    answer."""
    contract = yaml.safe_load(
        (base.ROOT / "contracts" / contract_file).read_text(encoding="utf-8")
    )
    for entry in contract["dod_evidence"]:
        for check in entry["checks"]:
            value = str(check["check_value"])
            assert not re.search(r"\bssh\s+h201\b", value), entry["id"]
            if "on_host " in value:
                assert "host=${RULING_REGISTER_HOST:-h201}" in value, entry["id"]
                assert "on_host true ||" in value, entry["id"]


@pytest.mark.parametrize(
    ("contract_file", "ac", "gate_check"),
    [
        ("OMN-25318.yaml", "AC1", "dod-omn24944-ac4-integration-proof"),
        ("OMN-25377.yaml", "AC1", "dod-omn25377-integration-proof"),
    ],
)
def test_the_evidence_falsifiers_name_the_local_gate_check_and_no_lab_line(
    contract_file: str, ac: str, gate_check: str
) -> None:
    """Plain pytest reads stubs and the example overlay and passes, so it cannot falsify evidence on h201: the falsifier
    names the local Done gate check that reads it, and the AC4 check is not said to need a Lab line."""
    statement = _acceptance(contract_file, ac)
    falsifier = statement.split(" -- falsifier: ", 1)[1]
    assert not falsifier.startswith("`uv run pytest")
    assert f"the local Done gate check {gate_check}" in falsifier
    if contract_file == "OMN-25318.yaml":
        assert "Lab" not in statement
        contract = yaml.safe_load(
            (base.ROOT / "contracts" / contract_file).read_text(encoding="utf-8")
        )
        entry = next(
            d for d in contract["dod_evidence"] if d["id"] == "dod-omn25318-ac1"
        )
        assert "without a Lab line" not in str(entry["description"])


def test_the_serial_proof_runs_under_this_hosts_onex_state_dir_and_drops_a_named_lock(
    tmp_path: Path,
) -> None:
    """The serial proof's tick takes the lock the unit takes: it runs under ONEX_STATE_DIR with no lock variable of its
    own, so on h201 it and the unit's tick exclude each other; with no ONEX_STATE_DIR it refuses to run."""
    world = ProofWorld(tmp_path)
    ok = world.run(0)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    seen = (tmp_path / "uv-env.txt").read_text(encoding="utf-8").splitlines()
    assert seen == [f"ONEX_STATE_DIR={tmp_path / 'state'}", "LOCK="]
    (tmp_path / "uv-env.txt").unlink()
    refused = world.run(0, drop=("ONEX_STATE_DIR",))
    assert refused.returncode != 0
    assert "ONEX_STATE_DIR" in refused.stderr
    assert not (tmp_path / "uv-env.txt").exists()
