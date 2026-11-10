# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_installer_registry_units: a registry_module_tick unit runs a branch checkout when its contract declares the override."""

from __future__ import annotations

import importlib
import json
import os
import plistlib
import signal
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_host_installer_registry_units"
UNIT = "factory-floor-publish"


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, env=scrub_git_location_env(os.environ), **kw)


def real_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Real git and id; every launchctl and uv proof is recorded and answered with success."""
    if argv[0] in ("git", "id"):
        return _run(argv, capture_output=True, text=True, check=False)
    return subprocess.CompletedProcess(argv, 0, "", "")


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return real_run(argv)


def make_checkout(
    path: Path,
    entry: str = "src/omnimarket/nodes/node_factory_floor_publish_effect/__main__.py",
) -> str:
    path.mkdir(parents=True)
    _run(["git", "-C", str(path), "init", "-q", "-b", "branch"], check=True)
    (path / entry).parent.mkdir(parents=True)
    (path / entry).write_text("print('tick')\n", encoding="utf-8")
    _run(["git", "-C", str(path), "add", "-A"], check=True)
    _run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "-m",
            "x",
        ],
        check=True,
    )
    return _run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def request(tmp_path: Path, **kw: Any) -> ModelRegistryUnitsRequest:
    fields: dict[str, Any] = {
        "unit": UNIT,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "workspace_config_root": str(tmp_path / "ws"),
        "plans_path": str(tmp_path / "plans"),
        "marketplace_root": str(tmp_path / "market"),
        "runtime_ssh_host": "lab-runtime",
        "ledger_host_name": "lab-ledger-host",
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest(**fields)


@pytest.fixture
def canonical(tmp_path: Path) -> Path:
    path = tmp_path / "omni" / "omnimarket"
    make_checkout(path)
    return path


BREW_PYTHON = "/opt/homebrew/bin/python3.13"


@pytest.fixture(autouse=True)
def brew_python_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host under test has the brew interpreter (CI is not a Mac); the refusal test removes it."""
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


def plist_of(text: str) -> dict[str, Any]:
    loaded: dict[str, Any] = plistlib.loads(text.encode("utf-8"))
    return loaded


def test_without_an_override_the_unit_runs_the_canonical_clone(
    tmp_path: Path, canonical: Path
) -> None:
    result = HandlerRegistryUnits(real_run).handle(request(tmp_path))
    assert result.status == "success", result.stderr
    assert result.project_is_canonical
    args = plist_of(result.rendered)["ProgramArguments"]
    assert args[args.index("--project") + 1] == str(canonical)


def test_with_the_override_the_rendered_unit_runs_the_branch_checkout_and_not_the_canonical_clone(
    tmp_path: Path, canonical: Path
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    result = HandlerRegistryUnits(real_run).handle(
        request(tmp_path, project_path=str(branch), expected_revision=revision)
    )
    assert result.status == "success", result.stderr
    assert not result.project_is_canonical
    assert result.revision == revision
    args = plist_of(result.rendered)["ProgramArguments"]
    assert args[args.index("--project") + 1] == str(branch)
    # the falsifier: no argument of the rendered unit may still name the canonical clone path
    assert all(str(canonical) not in arg for arg in args)


def test_an_override_that_is_dirty_at_the_wrong_revision_or_without_the_entry_file_is_refused(
    tmp_path: Path, canonical: Path
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    handler = HandlerRegistryUnits(real_run)
    wrong = handler.handle(
        request(tmp_path, project_path=str(branch), expected_revision="0" * 12)
    )
    assert wrong.status == "failure"
    assert "not the expected revision" in wrong.stderr
    (branch / "stray.txt").write_text("x", encoding="utf-8")
    dirty = handler.handle(
        request(tmp_path, project_path=str(branch), expected_revision=revision)
    )
    assert dirty.status == "failure"
    assert "not clean" in dirty.stderr
    bare = tmp_path / "bare"
    make_checkout(bare, entry="other/file.py")
    absent = handler.handle(request(tmp_path, project_path=str(bare)))
    assert absent.status == "failure"
    assert "does not carry" in absent.stderr
    relative = handler.handle(request(tmp_path, project_path="branch"))
    assert relative.status == "failure"
    assert "not an absolute path" in relative.stderr


def test_a_unit_that_declares_no_override_refuses_one(
    tmp_path: Path, canonical: Path
) -> None:
    units = load_units()
    units[UNIT].pop("project_path_override")
    result = HandlerRegistryUnits(real_run, units).handle(
        request(tmp_path, project_path=str(tmp_path / "branch"))
    )
    assert result.status == "failure"
    assert "declares no project path override" in result.stderr


def test_only_a_declared_environment_path_may_be_overridden(
    tmp_path: Path, canonical: Path
) -> None:
    handler = HandlerRegistryUnits(real_run)
    ok = handler.handle(
        request(
            tmp_path, path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": "/installed/overlays"}
        )
    )
    assert (
        plist_of(ok.rendered)["EnvironmentVariables"]["ONEX_SKILL_OVERLAY_ROOTS"]
        == "/installed/overlays"
    )
    refused = handler.handle(
        request(tmp_path, path_overrides={"ONEX_LEDGER_PATH": "/elsewhere"})
    )
    assert refused.status == "failure"
    assert "not a path override" in refused.stderr


def test_a_placeholder_the_request_does_not_set_is_refused(
    tmp_path: Path, canonical: Path
) -> None:
    result = HandlerRegistryUnits(real_run).handle(
        request(tmp_path, workspace_config_root="")
    )
    assert result.status == "failure"
    assert "workspace_config_root" in result.stderr


def test_install_writes_the_plist_loads_it_and_records_the_revision_it_runs(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = (
        tmp_path
        / "home"
        / "Library/LaunchAgents"
        / "ai.omninode.factory-floor-publish.plist"
    )
    args = plist_of(plist.read_text(encoding="utf-8"))["ProgramArguments"]
    assert args[args.index("--project") + 1] == str(branch)
    record = json.loads(Path(result.record).read_text(encoding="utf-8"))
    assert record["revision"] == revision
    assert record["project"] == str(branch)
    assert not record["project_is_canonical"]
    verbs = [c[1] for c in run.calls if c[0] == "launchctl"]
    assert verbs == ["bootout", "bootstrap"]
    proofs = [c for c in run.calls if c[0] == "env" and "/usr/bin/uv" in c]
    assert proofs
    assert str(branch) in proofs[0]
    assert "--no-sync" not in proofs[0]


def test_the_rendered_unit_and_its_proof_run_on_the_brew_interpreter(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A uv-managed CPython never gets the macOS Local Network grant: under launchd its LAN connections fail with EHOSTUNREACH."""
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(
        request(
            tmp_path,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    args = plist_of(result.rendered)["ProgramArguments"]
    assert args[args.index("--python") + 1] == BREW_PYTHON
    assert args.index("--python") < args.index("--project")
    proof = next(c for c in run.calls if c[0] == "env" and "/usr/bin/uv" in c)
    assert proof[proof.index("--python") + 1] == BREW_PYTHON


def test_a_host_without_a_declared_interpreter_is_refused(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda _path: False,
    )
    result = HandlerRegistryUnits(real_run).handle(request(tmp_path))
    assert result.status == "failure"
    assert "Local Network grant" in result.stderr


def test_an_unknown_unit_is_refused(tmp_path: Path) -> None:
    result = HandlerRegistryUnits(real_run).handle(
        request(tmp_path, unit="no-such-unit")
    )
    assert result.status == "failure"
    assert result.exit_code == 2


def test_the_contract_declares_the_override_and_the_node_is_registered() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == "node_host_installer_registry_units"
    unit = contract["units"][UNIT]
    assert unit["kind"] == "registry_module_tick"
    assert unit["project_path_override"]["request_field"] == "project_path"
    assert hasattr(
        importlib.import_module(contract["handler"]["module"]),
        contract["handler"]["name"],
    )
    assert hasattr(
        importlib.import_module(contract["entrypoint"]["module"]),
        contract["entrypoint"]["name"],
    )
    assert (NODE_DIR / unit["template"]).is_file()
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert (
        pyproject["project"]["entry-points"]["onex.nodes"][
            "node_host_installer_registry_units"
        ]
        == "omnimarket.nodes.node_host_installer_registry_units"
    )
    assert (
        yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))["name"]
        == contract["name"]
    )


HARNESS_UNIT = "lab-test-harness"
HARNESS_ENTRY = (
    "src/omnimarket/nodes/node_lab_test_harness_effect/runtime_lab_test_harness.py"
)


def test_the_central_test_harness_unit_runs_its_timer_module_hourly_from_a_branch_checkout(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=HARNESS_ENTRY)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=HARNESS_UNIT,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == "ai.omninode.lab-test-harness"
    assert plist["StartInterval"] == 3600
    assert plist["RunAtLoad"] is True
    args = plist["ProgramArguments"]
    assert args[args.index("-m") + 1] == (
        "omnimarket.nodes.node_lab_test_harness_effect.runtime_lab_test_harness"
    )
    assert args[args.index("--project") + 1] == str(branch)
    assert args[args.index("--python") + 1] == BREW_PYTHON
    environment = plist["EnvironmentVariables"]
    assert environment["ONEX_STATE_DIR"] == str(tmp_path / "omni" / ".onex_state")
    assert environment["OMNIBASE_PATH"] == str(tmp_path / "omni")
    record = json.loads(
        (
            tmp_path
            / "omni"
            / ".onex_state"
            / HARNESS_UNIT
            / f"{HARNESS_UNIT}.installed.json"
        ).read_text()
    )
    assert record["revision"] == revision
    assert record["project_is_canonical"] is False


def test_the_central_test_harness_unit_refuses_a_checkout_without_its_timer_module(
    tmp_path: Path, canonical: Path
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)  # carries only the floor's entry file
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit=HARNESS_UNIT,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "failure"


class Overlap:
    """A runner that, on its first launchctl bootout, sends a second request for the unit while the first install runs."""

    def __init__(self, second: Any) -> None:
        self.second = second
        self.nested: Any = None

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["launchctl", "bootout"] and self.nested is None:
            self.nested = self.second()
        return real_run(argv)


def test_a_second_install_of_the_unit_is_refused_while_the_first_runs(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    install = request(
        tmp_path, mode="install", project_path=str(branch), expected_revision=revision
    )
    other = Recorder()
    overlap = Overlap(lambda: HandlerRegistryUnits(other).handle(install))
    first = HandlerRegistryUnits(overlap).handle(install)
    assert first.status == "success", first.stderr
    assert overlap.nested.exit_code == 75
    assert overlap.nested.status == "failure"
    assert "INSTALL-IN-PROGRESS" in overlap.nested.stderr
    assert f"pid={os.getpid()}" in overlap.nested.stderr
    assert other.calls == []


def test_the_lock_is_released_when_an_install_fails_so_the_next_one_runs(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    refused = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            mode="install",
            project_path=str(branch),
            expected_revision="0" * 40,
        )
    )
    assert refused.exit_code == 1
    retry = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert retry.status == "success", retry.stderr


def test_a_dry_run_and_a_status_read_never_take_the_lock(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    install = request(
        tmp_path, mode="install", project_path=str(branch), expected_revision=revision
    )
    reads: list[Any] = []

    def read_while_installing() -> Any:
        for mode in ("dry-run", "status"):
            reads.append(
                HandlerRegistryUnits(Recorder()).handle(
                    request(
                        tmp_path,
                        mode=mode,
                        project_path=str(branch),
                        expected_revision=revision,
                    )
                )
            )
        return reads[-1]

    first = HandlerRegistryUnits(Overlap(read_while_installing)).handle(install)
    assert first.status == "success", first.stderr
    assert reads[0].exit_code == 0
    assert all("INSTALL-IN-PROGRESS" not in r.stderr for r in reads)


def test_installs_of_two_different_units_never_take_the_same_lock(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    harness = tmp_path / "harness"
    harness_revision = make_checkout(harness, entry=HARNESS_ENTRY)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    install = request(
        tmp_path, mode="install", project_path=str(branch), expected_revision=revision
    )
    other_unit = request(
        tmp_path,
        unit=HARNESS_UNIT,
        mode="install",
        project_path=str(harness),
        expected_revision=harness_revision,
    )
    overlap = Overlap(lambda: HandlerRegistryUnits(Recorder()).handle(other_unit))
    first = HandlerRegistryUnits(overlap).handle(install)
    assert first.status == "success", first.stderr
    assert overlap.nested.status == "success", overlap.nested.stderr
    assert overlap.nested.exit_code == 0
    assert "INSTALL-IN-PROGRESS" not in overlap.nested.stderr


def test_the_lock_dies_with_its_holder_process_so_the_next_install_runs(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    state = tmp_path / "omni" / str(load_units()[UNIT]["state_directory"])
    state.mkdir(parents=True, exist_ok=True)
    lock = state / f"{UNIT}.install.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys, time\n"
            "handle = open(sys.argv[1], 'a+')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('held', flush=True)\n"
            "time.sleep(600)\n",
            str(lock),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        install = request(
            tmp_path,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
        recorder = Recorder()
        refused = HandlerRegistryUnits(recorder).handle(install)
        assert refused.exit_code == 75
        assert "INSTALL-IN-PROGRESS" in refused.stderr
        assert recorder.calls == []
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=30)
        retry = HandlerRegistryUnits(Recorder()).handle(install)
        assert retry.status == "success", retry.stderr
    finally:
        if holder.poll() is None:
            holder.send_signal(signal.SIGKILL)
            holder.wait(timeout=30)
        if holder.stdout is not None:
            holder.stdout.close()


PARITY_UNIT = "work-ledger-parity-receipt"
PARITY_ENTRY = "src/omnimarket/nodes/node_work_ledger_parity_receipt_effect/runtime_parity_receipt.py"


def test_the_work_ledger_parity_receipt_unit_runs_its_node_module_hourly_with_the_parity_settings(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=PARITY_ENTRY)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=PARITY_UNIT,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "success", result.stderr
    plist = plist_of(result.rendered)
    assert plist["Label"] == "ai.omninode.work-ledger-parity-receipt"
    assert plist["StartInterval"] == 3600
    args = plist["ProgramArguments"]
    assert args[args.index("-m") + 1] == (
        "omnimarket.nodes.node_work_ledger_parity_receipt_effect.runtime_parity_receipt"
    )
    environment = plist["EnvironmentVariables"]
    assert environment["ONEX_LEDGER_PARITY_FILE"].startswith(str(tmp_path / "omni"))
    for name in (
        "ONEX_LEDGER_COMMAND",
        "ONEX_LEDGER_PARITY_PYTHON",
        "ONEX_LEDGER_PARITY_OVERLAY",
        "ONEX_LEDGER_PARITY_ENV_FILE",
        "ONEX_LEDGER_PARITY_PG_PORT",
    ):
        assert environment[name], name


def test_the_work_ledger_parity_receipt_unit_refuses_a_checkout_without_its_module(
    tmp_path: Path, canonical: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    branch = tmp_path / "branch"
    revision = make_checkout(
        branch, entry="src/omnimarket/nodes/other/runtime_other.py"
    )
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    result = HandlerRegistryUnits(Recorder()).handle(
        request(
            tmp_path,
            unit=PARITY_UNIT,
            mode="install",
            project_path=str(branch),
            expected_revision=revision,
        )
    )
    assert result.status == "failure"
