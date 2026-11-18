# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Proof for writing named store secrets to a private systemd environment file."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.nodes.node_secret_store_effect.__main__ import main
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_env_file import (
    HandlerSecretEnvFile,
)
from omnimarket.nodes.node_secret_store_effect.models.model_secret_env_file import (
    ModelSecretEnvFileRequest,
    ModelSecretEnvFileResult,
)
from tests.unit.nodes.node_secret_store_effect.test_handler_secret_store import (
    _FakeSecretStore,
    _handler,
)

pytestmark = pytest.mark.unit

_KEY = "floor_fill_live.store_dsn_env_file"
_NAME = "OMNIBASE_INFRA_DB_URL"
_FOLDER = "/shared/env"


@pytest.fixture
def value() -> str:
    return "postgresql://fake:" + secrets.token_hex(24) + "@localhost/fake"


def _write_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    overlay = tmp_path / "overlays" / "node_floor_fill_orchestrator" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        yaml.safe_dump(
            {"floor_fill_live": {"store_dsn_env": _NAME, "store_dsn_env_file": path}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay.parent.parent))


def _make_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    value: str | None,
) -> tuple[HandlerSecretEnvFile, _FakeSecretStore]:
    _write_overlay(tmp_path, monkeypatch, path)
    store = _FakeSecretStore(
        {_FOLDER + "/" + _NAME: value} if value is not None else {}
    )
    assert isinstance(store, ProtocolSecretStore)
    return HandlerSecretEnvFile(store_handler=_handler(store), home=tmp_path), store


def _request(**kwargs: str) -> ModelSecretEnvFileRequest:
    return ModelSecretEnvFileRequest(
        names=(_NAME,),
        folder=kwargs.get("folder", _FOLDER),
        overlay_key=kwargs.get("overlay_key", _KEY),
    )


def _no_value(value: str, *readings: str) -> None:
    for index in range(len(value) - 7):
        fragment = value[index : index + 8]
        if any(fragment in reading for reading in readings):
            pytest.fail("a secret fragment escaped the env file", pytrace=False)


async def test_secret_env_file_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    target = tmp_path / "private" / "floor-fill.env"
    target.parent.mkdir(mode=0o700)
    target.write_text("OBSOLETE=old\n", encoding="utf-8")
    target.chmod(0o600)
    original_inode = target.stat().st_ino
    handler, store = _make_handler(tmp_path, monkeypatch, str(target), value)

    real_open = os.open
    real_replace = os.replace
    created: list[tuple[str | bytes, int, int, int | None]] = []
    replaced: list[tuple[str | bytes, str | bytes, int | None, int | None]] = []

    def observe_open(
        path: str | bytes, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT:
            assert dir_fd is not None
            assert os.fstat(dir_fd).st_ino == target.parent.stat().st_ino
            created.append((path, mode, stat.S_IMODE(os.fstat(fd).st_mode), dir_fd))
        return fd

    def observe_replace(
        src: str | bytes,
        dst: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        assert target.stat().st_ino == original_inode
        replaced.append((src, dst, src_dir_fd, dst_dir_fd))
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "open", observe_open)
    monkeypatch.setattr(os, "replace", observe_replace)
    result = await handler.handle(_request())

    assert result.ok
    assert result.model_dump()["secrets"] == (
        {
            "name": _NAME,
            "present": True,
            "sha256": hashlib.sha256(value.encode()).hexdigest(),
        },
    )
    assert store.calls == [("get", _FOLDER + "/" + _NAME)]
    [(temporary_name, requested_mode, initial_mode, directory)] = created
    assert requested_mode == initial_mode == 0o600
    assert temporary_name != target.name
    assert replaced == [(temporary_name, target.name, directory, directory)]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    if target.read_bytes() != f"{_NAME}={value}\n".encode():
        pytest.fail(
            "the replacement did not contain exactly the expected assignment",
            pytrace=False,
        )
    captured = capsys.readouterr()
    _no_value(
        value,
        result.model_dump_json(),
        repr(result),
        caplog.text,
        captured.out,
        captured.err,
    )


@pytest.mark.parametrize("existing", [False, True])
async def test_secret_env_file_refusal_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
    existing: bool,
) -> None:
    target = tmp_path / "private" / "floor-fill.env"
    if existing:
        target.parent.mkdir(mode=0o700)
        target.write_text(value, encoding="utf-8")
        target.chmod(0o600)
        original_inode = target.stat().st_ino
    handler, _ = _make_handler(tmp_path, monkeypatch, str(target), None)
    caplog.set_level(logging.DEBUG)
    result = await handler.handle(_request())
    assert not result.ok
    assert result.model_dump()["secrets"] == (
        {"name": _NAME, "present": False, "sha256": None},
    )
    if existing:
        assert target.stat().st_ino == original_inode
        if target.read_text(encoding="utf-8") != value:
            pytest.fail("the existing file changed", pytrace=False)
        assert list(target.parent.iterdir()) == [target]
    else:
        assert not target.parent.exists()
    _no_value(value, result.model_dump_json(), caplog.text)


@pytest.mark.parametrize(
    "case",
    [
        "whitespace",
        "relative",
        "outside",
        "traversal",
        "symlink",
        "symlink-parent",
        "wide",
    ],
)
async def test_secret_env_file_refusal_unsafe_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
    case: str,
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = directory / "floor-fill.env"
    sentinel = tmp_path / "guard"
    sentinel.write_text("unchanged", encoding="utf-8")
    sentinel.chmod(0o600)
    path = str(target)
    if case == "whitespace":
        path += "\n"
    elif case == "relative":
        path = "private/floor-fill.env"
    elif case == "outside":
        path = str(tmp_path.parent / "outside" / "floor-fill.env")
    elif case == "traversal":
        path = str(directory / ".." / "guard")
    elif case == "symlink":
        target.symlink_to(sentinel)
    elif case == "symlink-parent":
        link = tmp_path / "link"
        link.symlink_to(directory, target_is_directory=True)
        path = str(link / target.name)
    elif case == "wide":
        target.write_text("unchanged", encoding="utf-8")
        target.chmod(0o640)

    handler, _ = _make_handler(tmp_path, monkeypatch, path, value)
    caplog.set_level(logging.DEBUG)
    original_entries = list(directory.iterdir())

    result = await handler.handle(_request())

    assert not result.ok
    assert list(directory.iterdir()) == original_entries
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    if case == "symlink":
        assert target.is_symlink()
    if (
        case == "wide"
    ):  # The effect refuses wider files, preserving their contents and mode.
        assert stat.S_IMODE(target.stat().st_mode) == 0o640
        assert target.read_text(encoding="utf-8") == "unchanged"
    _no_value(value, result.model_dump_json(), caplog.text)


async def test_secret_env_file_refusal_does_not_log_store_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    target = tmp_path / "private" / "floor-fill.env"
    handler, store = _make_handler(tmp_path, monkeypatch, str(target), value)
    # A store-provided error can contain part of a value and look like a status message.
    store.raise_on["get"] = PermissionError(
        "read refused with " + value.removeprefix("postgresql://fake:")[:12]
    )
    caplog.set_level(logging.DEBUG)

    result = await handler.handle(_request())

    assert result.status == "failed"
    assert not target.parent.exists()
    _no_value(value, result.model_dump_json(), caplog.text)


async def test_secret_env_file_refusal_cleans_up_after_rename_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    target = tmp_path / "private" / "floor-fill.env"
    target.parent.mkdir(mode=0o700)
    target.write_text("OLD=unchanged\n", encoding="utf-8")
    target.chmod(0o600)
    handler, _ = _make_handler(tmp_path, monkeypatch, str(target), value)

    def refuse_rename(*args: object, **kwargs: object) -> None:
        raise OSError("test rename failure")

    monkeypatch.setattr(os, "replace", refuse_rename)

    assert (await handler.handle(_request())).status == "failed"
    assert target.read_text(encoding="utf-8") == "OLD=unchanged\n"
    assert list(target.parent.iterdir()) == [target]


async def test_secret_env_file_contract_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.runtime.auto_wiring.discovery import _parse_contract
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    from omnimarket.nodes.node_secret_store_effect.handlers import (
        handler_secret_env_file,
    )

    contract_path = Path(handler_secret_env_file.__file__).parents[1] / "contract.yaml"
    contract = _parse_contract(
        contract_path=contract_path,
        entry_point_name="node_secret_store_effect",
        package_name="omnimarket",
        package_version="0.0.0",
    )
    assert contract.handler_routing is not None
    (route,) = [
        r for r in contract.handler_routing.handlers if r.operation == "write_env_file"
    ]
    assert route.handler.name == HandlerSecretEnvFile.__name__
    assert route.event_model is not None
    assert route.event_model.name == ModelSecretEnvFileRequest.__name__

    target = tmp_path / "private" / "floor-fill.env"
    handler, _ = _make_handler(tmp_path, monkeypatch, str(target), value)
    correlation = uuid4()
    request = _request().model_copy(update={"correlation_id": correlation})
    callback = _make_dispatch_callback(handler, event_model=route.event_model)
    dispatch = await callback(
        ModelEventEnvelope[object].model_validate_json(
            ModelEventEnvelope(
                payload=request,
                correlation_id=correlation,
                event_type=route.operation,
                source_tool="secret-env-file-contract-test",
            ).model_dump_json()
        )
    )

    assert dispatch is not None
    (event,) = dispatch.output_events
    receipt = ModelSecretEnvFileResult.model_validate(event)
    assert receipt.ok
    assert receipt.correlation_id == correlation
    assert set(receipt.secrets[0].model_dump()) == {"name", "present", "sha256"}
    _no_value(value, request.model_dump_json(), receipt.model_dump_json())


def test_secret_env_file_write_cli_fake_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    value: str,
) -> None:
    import asyncio

    from omnimarket.inference import local_byok_credential_adapter as local_adapter

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    store_file = tmp_path / "fake-store.sqlite"
    monkeypatch.setattr(local_adapter, "default_evidence_db_path", lambda: store_file)
    fake_store = local_adapter.LocalByokCredentialStore()
    assert asyncio.run(fake_store.set_secret(_NAME, value))
    assert fake_store.db_path == store_file
    onex_home = tmp_path / ".onex"
    onex_home.mkdir()
    monkeypatch.setattr(
        "omnibase_infra.doctor.delegation_doctor_support.default_onex_home",
        lambda: onex_home,
    )
    (onex_home / "config.yaml").write_text(
        "secret_store:\n  provider: local\n", encoding="utf-8"
    )
    target = tmp_path / "private" / "floor-fill.env"
    _write_overlay(tmp_path, monkeypatch, str(target))
    caplog.set_level(logging.DEBUG)

    exit_code = main(["write-env-file", "--overlay-key", _KEY, "--folder", _FOLDER])

    captured = capsys.readouterr()
    _no_value(value, captured.out, captured.err, caplog.text)
    result = ModelSecretEnvFileResult.model_validate_json(captured.out)
    assert exit_code == 0, result.status
    (reading,) = result.secrets
    assert reading.present
    assert reading.name == _NAME
    assert reading.sha256 == hashlib.sha256(value.encode()).hexdigest()
    mode = stat.S_IMODE(target.stat().st_mode)
    count = subprocess.run(
        ["grep", "-c", f"^{_NAME}=", str(target)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert mode == 0o600
    assert count == "1"
    print(f"count={count} mode={mode:o} sha256={reading.sha256}")
