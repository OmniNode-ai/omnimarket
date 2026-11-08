# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the additive in-place venv install, on a fixture venv."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_venv_atomic_install_effect.handlers import (
    handler_venv_atomic_install as handler_module,
)
from omnimarket.nodes.node_venv_atomic_install_effect.handlers.handler_venv_atomic_install import (
    HandlerVenvAtomicInstall,
)
from omnimarket.nodes.node_venv_atomic_install_effect.handlers.handler_venv_atomic_install_cli import (
    main as cli_main,
)
from omnimarket.nodes.node_venv_atomic_install_effect.handlers.venv_exchange import (
    exchange_paths,
)
from omnimarket.nodes.node_venv_atomic_install_effect.handlers.venv_layout import (
    read_lock,
)
from omnimarket.nodes.node_venv_atomic_install_effect.models.model_venv_atomic_install import (
    EnumVenvInstallStatus,
    ModelLockedPackage,
    ModelVenvAtomicInstallRequest,
    ModelVenvAtomicInstallResult,
)

pytestmark = pytest.mark.unit
NODE = Path(handler_module.__file__).parents[1]
COMMAND_TOPIC = "onex.cmd.omnimarket.venv-atomic-install-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.venv-atomic-install-completed.v1"
RELOCATABLE_SCRIPT = (
    '#!/bin/sh\n\'\'\'exec\' "$(dirname -- "$(realpath -- "$0")")"/\'{python}\' "$0" "$@"\n'
    "' '''\nprint('{name}')\n"
)


def write_dist(
    root: Path,
    name: str,
    version: str,
    files: dict[str, str],
    *,
    scripts: bool,
    site: bool,
) -> None:
    """One distribution under ``root``; ``site`` records scripts the way a live venv does."""
    record = []
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        record.append(rel)
    dist_info = root / f"{name}-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Name: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    record.append(f"{dist_info.name}/METADATA")
    if scripts:
        bin_dir = (root.parents[2] / "bin") if site else (root / "bin")
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / name
        script.write_text(
            RELOCATABLE_SCRIPT.format(
                python="python" if site else "/abs/live/bin/python",
                name=f"{name} {version}",
            ),
            encoding="utf-8",
        )
        record.append(f"../../../bin/{name}" if site else f"bin/{name}")
    record.append(f"{dist_info.name}/RECORD")
    (dist_info / "RECORD").write_text(
        "".join(f"{r},,\n" for r in record), encoding="utf-8"
    )


class Fixture:
    """A venv of alpha, beta, gamma and a namespace directory shared by two of them."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "venv"
        self.site = self.root / "lib" / "python3.12" / "site-packages"
        self.site.mkdir(parents=True)
        for name in ("alpha", "beta", "gamma"):
            write_dist(
                self.site,
                name,
                "1.0",
                {
                    f"{name}/__init__.py": "VERSION = '1.0'\n",
                    f"{name}/core.py": f"{name} = 1\n",
                    f"ns/{name}_part.py": f"{name}\n",
                },
                scripts=name == "beta",
                site=True,
            )
        self.staging = tmp / "venv.additive-staging"
        self.journal = tmp / "venv.additive.journal"
        self.installed: list[list[str]] = []

    def installer(self, version_staged: dict[str, str] | None = None):
        def install(
            request: ModelVenvAtomicInstallRequest, requirements: list[str]
        ) -> None:
            self.installed.append(list(requirements))
            for requirement in requirements:
                name, _, version = requirement.partition("==")
                version = (version_staged or {}).get(name, version)
                write_dist(
                    request.staging_dir,
                    name,
                    version,
                    {
                        f"{name}/__init__.py": f"VERSION = '{version}'\n",
                        f"{name}/core.py": f"{name} = {version}\n",
                        f"{name}/extra.py": "x = 1\n",
                        f"ns/{name}_part.py": f"{name} {version}\n",
                    },
                    scripts=name == "beta",
                    site=False,
                )

        return install

    def request(self, **moved: str) -> ModelVenvAtomicInstallRequest:
        locked = tuple(
            ModelLockedPackage(name=n, version=moved.get(n, "1.0"))
            for n in ("alpha", "beta", "gamma")
        )
        return ModelVenvAtomicInstallRequest(
            venv_root=self.root,
            site_packages=self.site,
            staging_dir=self.staging,
            journal_path=self.journal,
            locked=locked,
            python=self.root / "bin" / "python",
            installer_command=("uv",),
        )

    def listing(self) -> dict[str, tuple[str, int]]:
        """Every path under the venv with its content hash and inode: the whole file listing."""
        out: dict[str, tuple[str, int]] = {}
        for path in sorted(self.root.rglob("*")):
            digest = (
                "" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
            )
            out[str(path.relative_to(self.root))] = (digest, path.lstat().st_ino)
        return out

    def inode(self, rel: str) -> int:
        return (self.site / rel).lstat().st_ino


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_ac1_only_the_moved_package_is_installed_and_exchanged_in_place(
    fx: Fixture,
) -> None:
    alpha, gamma = fx.inode("alpha"), fx.inode("gamma")
    result = HandlerVenvAtomicInstall(installer=fx.installer()).handle(
        fx.request(beta="2.0")
    )
    assert result.status is EnumVenvInstallStatus.INSTALLED
    assert fx.installed == [["beta==2.0"]]
    assert [m.name for m in result.moved] == ["beta"]
    assert result.unchanged == 2
    assert (fx.site / "beta" / "__init__.py").read_text() == "VERSION = '2.0'\n"
    assert (fx.site / "beta" / "extra.py").exists()
    assert (fx.site / "beta-2.0.dist-info").is_dir()
    assert not (fx.site / "beta-1.0.dist-info").exists()
    assert (fx.site / "alpha").lstat().st_ino == alpha
    assert (fx.site / "gamma").lstat().st_ino == gamma
    assert "beta 2.0" in (fx.root / "bin" / "beta").read_text()
    assert "'python'" in (fx.root / "bin" / "beta").read_text()
    assert "/abs/live" not in (fx.root / "bin" / "beta").read_text()
    assert not fx.staging.exists()
    assert not fx.journal.exists()


def test_a_shared_namespace_directory_is_exchanged_file_by_file(fx: Fixture) -> None:
    ns = fx.inode("ns")
    HandlerVenvAtomicInstall(installer=fx.installer()).handle(fx.request(beta="2.0"))
    assert fx.inode("ns") == ns
    assert (fx.site / "ns" / "beta_part.py").read_text() == "beta 2.0\n"
    assert (fx.site / "ns" / "alpha_part.py").read_text() == "alpha\n"


def test_nothing_moved_stages_and_touches_nothing(fx: Fixture) -> None:
    before = fx.listing()
    result = HandlerVenvAtomicInstall(installer=fx.installer()).handle(fx.request())
    assert result.status is EnumVenvInstallStatus.NOOP
    assert fx.installed == []
    assert not fx.staging.exists()
    assert fx.listing() == before


def test_ac2_failure_after_the_first_exchange_restores_the_listing(fx: Fixture) -> None:
    before = fx.listing()
    calls: list[tuple[Path, Path]] = []

    def failing_after_first(live: Path, stage: Path) -> None:
        calls.append((live, stage))
        if len(calls) == 2:
            raise OSError("injected failure after the first exchange")
        exchange_paths(live, stage)

    result = HandlerVenvAtomicInstall(
        installer=fx.installer(), exchange=failing_after_first
    ).handle(fx.request(alpha="2.0", beta="2.0"))
    assert result.status is EnumVenvInstallStatus.ROLLED_BACK
    assert "injected failure" in result.reason
    assert result.exchanged_units
    assert result.restored_units
    assert fx.listing() == before
    assert not fx.staging.exists()
    assert not fx.journal.exists()


def test_ac2_control_without_the_journal_restore_the_listing_differs(
    fx: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = fx.listing()
    monkeypatch.setattr(handler_module, "roll_back", lambda *_unused: [])
    calls: list[int] = []

    def failing_after_first(live: Path, stage: Path) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("injected failure after the first exchange")
        exchange_paths(live, stage)

    result = HandlerVenvAtomicInstall(
        installer=fx.installer(), exchange=failing_after_first
    ).handle(fx.request(alpha="2.0", beta="2.0"))
    assert result.status is EnumVenvInstallStatus.ROLLED_BACK
    assert fx.listing() != before


def test_a_readback_that_misses_the_locked_version_rolls_back(fx: Fixture) -> None:
    before = fx.listing()
    result = HandlerVenvAtomicInstall(installer=fx.installer({"beta": "1.5"})).handle(
        fx.request(beta="2.0")
    )
    assert result.status is EnumVenvInstallStatus.ROLLED_BACK
    assert "readback" in result.reason
    assert fx.listing() == before


def test_an_installer_failure_leaves_the_live_venv_untouched(fx: Fixture) -> None:
    before = fx.listing()

    def broken(request: ModelVenvAtomicInstallRequest, requirements: list[str]) -> None:
        raise handler_module.VenvInstallError("no index")

    result = HandlerVenvAtomicInstall(installer=broken).handle(fx.request(beta="2.0"))
    assert result.status is EnumVenvInstallStatus.FAILED_BEFORE_EXCHANGE
    assert fx.listing() == before
    assert not fx.staging.exists()


def test_a_journal_left_by_a_dead_pass_is_undone_before_the_next_pass(
    fx: Fixture,
) -> None:
    before = fx.listing()

    class Died(BaseException):
        pass

    calls: list[int] = []

    def dies_after_first(live: Path, stage: Path) -> None:
        calls.append(1)
        exchange_paths(live, stage)
        if len(calls) == 2:
            raise Died

    with pytest.raises(Died):
        HandlerVenvAtomicInstall(
            installer=fx.installer(), exchange=dies_after_first
        ).handle(fx.request(alpha="2.0", beta="2.0"))
    assert fx.journal.exists()
    assert fx.listing() != before
    result = HandlerVenvAtomicInstall(installer=fx.installer()).handle(fx.request())
    assert result.status is EnumVenvInstallStatus.NOOP
    assert result.recovered_units
    assert fx.listing() == before
    assert not fx.journal.exists()


def test_a_git_sourced_package_moves_when_its_commit_moves_at_the_same_version(
    fx: Fixture,
) -> None:
    (fx.site / "alpha-1.0.dist-info" / "direct_url.json").write_text(
        '{"url": "git+https://example.invalid/alpha", "vcs_info": {"vcs": "git", "commit_id": "aaa"}}',
        encoding="utf-8",
    )
    request = fx.request().model_copy(
        update={
            "locked": (
                ModelLockedPackage(
                    name="alpha", version="1.0", spec="alpha==1.0", commit="bbb"
                ),
            )
        }
    )
    unmoved = request.model_copy(
        update={
            "locked": (ModelLockedPackage(name="alpha", version="1.0", commit="aaa"),)
        }
    )
    assert (
        HandlerVenvAtomicInstall(installer=fx.installer()).handle(unmoved).status
        is EnumVenvInstallStatus.NOOP
    )
    result = HandlerVenvAtomicInstall(installer=fx.installer()).handle(request)
    assert [m.commit for m in result.moved] == ["bbb"]
    assert fx.installed == [["alpha==1.0"]]
    # The fake installer stages no direct_url.json, so the readback refuses and the pass rolls back.
    assert result.status is EnumVenvInstallStatus.ROLLED_BACK
    assert (fx.site / "alpha-1.0.dist-info" / "direct_url.json").exists()


def test_the_lock_reader_takes_registry_packages_only(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\n[[package]]\nname = "Foo_Bar"\nversion = "1.2"\nsource = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "self"\nversion = "0.1"\nsource = { editable = "." }\n',
        encoding="utf-8",
    )
    assert [(p.name, p.version) for p in read_lock(lock)] == [("foo-bar", "1.2")]


def test_the_cli_refuses_a_venv_without_one_site_packages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    code = cli_main(
        [
            "--venv-root",
            str(tmp_path),
            "--lock-file",
            str(tmp_path / "uv.lock"),
            "--staging-dir",
            str(tmp_path / "s"),
            "--journal",
            str(tmp_path / "j"),
            "--uv",
            "uv",
        ]
    )
    assert code == 3
    assert "VENV-ATOMIC-INSTALL-REFUSED" in capsys.readouterr().err


def test_the_contract_declares_the_command_and_terminal_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC


async def run_over_bus(
    request: ModelVenvAtomicInstallRequest, handler: HandlerVenvAtomicInstall
) -> ModelVenvAtomicInstallResult:
    """The request crosses the in-memory bus; the terminal event carries the result."""
    bus = EventBusInmemory()
    await bus.start()
    terminal: list[ModelVenvAtomicInstallResult] = []

    async def on_command(message) -> None:
        result = handler.handle(
            ModelVenvAtomicInstallRequest.model_validate_json(message.value)
        )
        await bus.publish(
            TERMINAL_TOPIC, key=b"venv", value=result.model_dump_json().encode()
        )

    async def on_terminal(message) -> None:
        terminal.append(ModelVenvAtomicInstallResult.model_validate_json(message.value))

    try:
        await bus.subscribe(
            COMMAND_TOPIC, group_id="venv-atomic-test", on_message=on_command
        )
        await bus.subscribe(
            TERMINAL_TOPIC, group_id="venv-atomic-terminal", on_message=on_terminal
        )
        await bus.publish(
            COMMAND_TOPIC, key=b"venv", value=request.model_dump_json().encode()
        )
    finally:
        await bus.close()
    assert len(terminal) == 1
    return terminal[0]


@pytest.mark.asyncio
async def test_golden_chain_event_installed(fx: Fixture) -> None:
    result = await run_over_bus(
        fx.request(beta="2.0"), HandlerVenvAtomicInstall(installer=fx.installer())
    )
    assert result.status is EnumVenvInstallStatus.INSTALLED
    assert (fx.site / "beta-2.0.dist-info").is_dir()


@pytest.mark.asyncio
async def test_error_chain_event_rolled_back(fx: Fixture) -> None:
    before = fx.listing()
    calls: list[int] = []

    def failing_after_first(live: Path, stage: Path) -> None:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("injected failure after the first exchange")
        exchange_paths(live, stage)

    handler = HandlerVenvAtomicInstall(
        installer=fx.installer(), exchange=failing_after_first
    )
    result = await run_over_bus(fx.request(alpha="2.0", beta="2.0"), handler)
    assert result.status is EnumVenvInstallStatus.ROLLED_BACK
    assert fx.listing() == before


def test_exchange_paths_swaps_a_directory_and_a_file(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    (a / "x").write_text("dir")
    b.write_text("file")
    exchange_paths(a, b)
    assert a.read_text() == "file"
    assert (b / "x").read_text() == "dir"
    assert os.path.lexists(tmp_path / "a.exchange-parked") is False
