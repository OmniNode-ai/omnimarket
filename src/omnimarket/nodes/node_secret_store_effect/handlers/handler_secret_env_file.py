# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve through the secret-store handler and replace a declared EnvironmentFile."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models.model_secret_env_file import (
    ModelSecretEnvFileReading,
    ModelSecretEnvFileRequest,
    ModelSecretEnvFileResult,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_UNIT_PATH = re.compile(r"/[A-Za-z0-9_@+=:,./~-]+")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INVALID_ENV_CHARS = re.compile(r"[\x00\r\n\ufdd0-\ufdef\ufeff]")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_MODE = 0o600


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    overlay: str
    names_from: tuple[str, ...]
    folder: str


def _destination(raw: object, home: Path) -> Path:
    if not isinstance(raw, str) or not _UNIT_PATH.fullmatch(raw):
        raise ValueError("invalid unit path")
    if any(part in ("", ".", "..") for part in raw[1:].split("/")):
        raise ValueError("path must be canonical")
    path = Path(raw)
    if len(path.relative_to(home).parts) < 2:
        raise ValueError("path must be in a directory under the unit home")
    return path


def _load_binding(
    request: ModelSecretEnvFileRequest, contract_path: Path, home: Path
) -> tuple[Path, tuple[str, ...]]:
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    bindings = TypeAdapter(dict[str, _Binding]).validate_python(
        contract["secret_env_files"]
    )
    binding = bindings[request.overlay_key]
    if request.folder != binding.folder:
        raise ValueError("folder differs from the declared binding")
    overlay_path = find_node_overlay(binding.overlay)
    if overlay_path is None:
        raise ValueError("overlay is absent")
    section, key = request.overlay_key.split(".")
    overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))[section]
    names = TypeAdapter(tuple[str, ...]).validate_python(
        tuple(overlay[name] for name in binding.names_from), strict=True
    )
    if (
        len(set(names)) != len(names)
        or not names
        or not all(_ENV_NAME.fullmatch(name) for name in names)
    ):
        raise ValueError("invalid environment names")
    if request.names is not None and request.names != names:
        raise ValueError("names differ from the declared binding")
    return _destination(overlay[key], home), names


def _assignment(name: str, value: str) -> bytes:
    """Render one UTF-8 assignment using systemd's EnvironmentFile quoting."""
    if _INVALID_ENV_CHARS.search(value) or any(
        (ord(char) & 0xFFFF) in (0xFFFE, 0xFFFF) for char in value
    ):
        raise ValueError("the secret cannot be represented on a single env-file line")
    if value != value.strip(" \t") or value.startswith(("'", '"')) or "\\" in value:
        value = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name}={value}\n".encode()


@contextmanager
def _open_directory(path: Path, home: Path) -> Iterator[int]:
    """Walk without following links and pin the directory through the rename."""
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        home_depth = len(home.parts)
        for depth, component in enumerate(path.parts[1:], start=2):
            try:
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if depth <= home_depth:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(component, 0o700, dir_fd=fd)
                next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if depth >= home_depth:
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise PermissionError("directory must be owned by the unit user")
        yield fd
    finally:
        os.close(fd)


def _check_existing(directory: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != _FILE_MODE
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        raise PermissionError("existing env file must be a private regular file")


def _replace_file(path: Path, home: Path, lines: list[bytes]) -> None:
    with _open_directory(path.parent, home) as directory:
        _check_existing(directory, path.name)
        temp = f".env-{secrets.token_hex(16)}.tmp"
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            _FILE_MODE,
            dir_fd=directory,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                # A restrictive umask can only make the newly created file narrower.
                if stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != _FILE_MODE:
                    os.fchmod(stream.fileno(), _FILE_MODE)
                stream.writelines(lines)
                stream.flush()
                os.fsync(stream.fileno())
            _check_existing(directory, path.name)
            os.replace(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temp, dir_fd=directory)
            raise


class HandlerSecretEnvFile:
    """Write only a locally declared binding, with no value in the receipt."""

    def __init__(
        self,
        *,
        store_handler: HandlerSecretStore | None = None,
        home: Path | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._home = home or Path.home()
        self._contract_path = contract_path or _CONTRACT
        self._store_handler = store_handler or HandlerSecretStore()

    async def handle(
        self, request: ModelSecretEnvFileRequest
    ) -> ModelSecretEnvFileResult:
        readings: list[ModelSecretEnvFileReading] = []

        def result(
            status: Literal["written", "refused", "missing", "failed"],
        ) -> ModelSecretEnvFileResult:
            return ModelSecretEnvFileResult(
                status=status,
                secrets=tuple(readings),
                correlation_id=request.correlation_id,
            )

        try:
            path, names = _load_binding(request, self._contract_path, self._home)
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError):
            return result("refused")

        lines: list[bytes] = []
        for name in names:
            try:
                resolved = await self._store_handler.handle(
                    ModelSecretStoreRequest(
                        operation=EnumSecretStoreOperation.RESOLVE,
                        folder=request.folder,
                        key=name,
                        correlation_id=request.correlation_id,
                    ),
                    report_detail=False,
                )
            except Exception:
                # A store exception may embed request or response data.
                return result("failed")
            if resolved.outcome == EnumSecretStoreOutcome.NOT_FOUND:
                readings.append(
                    ModelSecretEnvFileReading(name=name, present=False, sha256=None)
                )
                continue
            if not resolved.ok or resolved.value is None:
                return result("failed")
            value = resolved.value.get_secret_value()
            try:
                lines.append(_assignment(name, value))
                digest = hashlib.sha256(value.encode()).hexdigest()
            except ValueError:
                return result("refused")
            readings.append(
                ModelSecretEnvFileReading(name=name, present=True, sha256=digest)
            )
        if any(not reading.present for reading in readings):
            return result("missing")

        try:
            _replace_file(path, self._home, lines)
        except (PermissionError, NotADirectoryError):
            return result("refused")
        except OSError:
            return result("failed")
        return result("written")
