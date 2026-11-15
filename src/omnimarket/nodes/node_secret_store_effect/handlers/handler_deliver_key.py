# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generated keys delivered into a host env file, value unseen (OMN-24611).

``handle(request: ModelDeliverKeyRequest) -> ModelDeliverKeyResult``. A
generated key the contract declares is read from the store through
:class:`HandlerSecretStore`, the node's one store seam, and written as one
``<env_name>=<value>`` line into the file the contract's ``env_file_deliveries``
entry names under the user's home, mode 0600, so the dev deploy agent's unit
reads its signing key through ``EnvironmentFile=`` and no person types or sees
the value. Both the delivery and every one of its fields (key_id, path, env
name, mode) are the contract's, so none is written in code.

The value is never printed, serialized, logged or put in an exception
---------------------------------------------------------------------
The result model has no field that can hold it. The value lives in a local
variable from the store read to the ``os.replace`` and is then only in the
store and the file. Logs and ``detail`` name the id, the env NAME, the file's
path and the outcome, never the value; an ``OSError`` is reported by its class
name alone (the store handler's own posture).

Every refusal is a typed outcome and nothing is written
-------------------------------------------------------
An id the contract's ``env_file_deliveries`` does not declare, or whose
``key_id`` no ``generated_keys`` entry backs, is ``not_declared``: nothing is
read and nothing is written. A key the store does not return is
``key_unreadable`` with the store's outcome in the detail. A value that is not
``[A-Za-z0-9_-]+`` -- what systemd's ``EnvironmentFile=`` parses without
quoting -- is ``value_unsafe``. An ``OSError`` while writing is
``write_failed``. Writing the file is atomic: a temp file in the target's own
directory opened ``O_WRONLY|O_CREAT|O_EXCL`` mode 0600, written, fsynced,
``os.replace``d over the target, then chmodded to the declared mode. A file
that already holds exactly the declared line is ``unchanged`` and is not
rewritten, but the declared mode is enforced on every run: a file left at
any other mode is chmodded to the declared one (detail names the
correction), and an ``OSError`` while statting or chmodding is
``write_failed``. A delivery whose ``home_relative_path`` is absolute or
contains ``..``, or whose ``mode`` is zero or wider than 0600, is refused
when the contract is loaded.

The machine identity is read exactly as ``handler_generate_key`` reads it: the
machine's own ``secret_store`` block first, then, with no block, the store
addressing and identity the host's own ``~/.omnibase/.env`` already carries.
With an overlay document the store block is that document's and the host env
file fallback never runs.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import yaml

from omnimarket.models.secret_store.model_deliver_key_request import (
    ModelDeliverKeyRequest,
)
from omnimarket.models.secret_store.model_deliver_key_result import (
    EnumDeliverKeyOutcome,
    ModelDeliverKeyResult,
)
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_secret_store_effect.adapters import IdentityFileStore
from omnimarket.nodes.node_secret_store_effect.handlers.handler_house_model_key import (
    HOST_ENV_FILE,
    host_env_overlay,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
#: What systemd's EnvironmentFile= parses as an unquoted assignment value.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class _GeneratedFolderKey:
    """The folder and key NAME a ``generated_keys`` entry stores its value under."""

    folder: str
    key: str


@dataclass(frozen=True)
class _DeclaredDelivery:
    """One ``env_file_deliveries`` entry: every value comes from the contract."""

    key_id: str
    home_relative_path: str
    env_name: str
    mode: int


def _load_contract(
    contract_path: Path,
) -> tuple[dict[str, _DeclaredDelivery], dict[str, _GeneratedFolderKey]]:
    """The contract's env file deliveries, its generated keys alongside them.

    Raises:
        ValueError: the contract is not a mapping, a delivery entry is
            malformed, its ``home_relative_path`` is absolute or contains
            ``..``, or its ``mode`` is zero or has any bit outside 0600.
    """
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path}: the contract is not a mapping")
    generated_block = raw.get("generated_keys") or {}
    generated: dict[str, _GeneratedFolderKey] = {}
    for name, spec in generated_block.items():
        if not isinstance(spec, dict):
            raise ValueError(f"{contract_path}: generated key {name!r} malformed")
        generated[str(name)] = _GeneratedFolderKey(
            folder=str(spec["folder"]),
            key=str(spec["key"]),
        )
    block = raw.get("env_file_deliveries") or {}
    declared: dict[str, _DeclaredDelivery] = {}
    for name, spec in block.items():
        if not isinstance(spec, dict):
            raise ValueError(f"{contract_path}: env file delivery {name!r} malformed")
        relative = Path(str(spec["home_relative_path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(
                f"{contract_path}: env file delivery {name!r} names a path "
                "outside the user's home"
            )
        mode = int(str(spec["mode"]), 8)
        if mode == 0 or mode & ~0o600:
            raise ValueError(
                f"{contract_path}: env file delivery {name!r} declares a mode "
                "wider than 0600"
            )
        declared[str(name)] = _DeclaredDelivery(
            key_id=str(spec["key_id"]),
            home_relative_path=relative.as_posix(),
            env_name=str(spec["env_name"]),
            mode=mode,
        )
    return declared, generated


class HandlerDeliverKeyToEnvFile:
    """EFFECT: deliver one declared key's stored value into a host env file.

    Args:
        store_handler: The secret store handler to ask. With none, the overlay
            document's store block when one is given, else the machine's own
            overlay first and the host env file second.
        overlay: The store block to resolve through; with it, the host env file
            fallback never runs.
        onex_home: The ONEX home holding ``config.yaml``; defaults to the standard one.
        home: The user's home holding ``.omnibase/.env`` and the delivery
            target; defaults to ``Path.home()``.
        contract_path: The node contract, for ``env_file_deliveries`` and
            ``generated_keys``.
    """

    def __init__(
        self,
        *,
        store_handler: HandlerSecretStore | None = None,
        overlay: ModelSecretStoreOverlay | None = None,
        onex_home: Path | None = None,
        home: Path | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._store_handler = store_handler
        self._overlay = overlay
        self._onex_home = onex_home
        self._home = home
        self._deliveries, self._generated = _load_contract(
            contract_path or _CONTRACT_PATH
        )

    async def handle(self, request: ModelDeliverKeyRequest) -> ModelDeliverKeyResult:
        def result(
            outcome: EnumDeliverKeyOutcome,
            *,
            env_name: str | None = None,
            home_relative_path: str | None = None,
            key_length: int | None = None,
            detail: str | None = None,
        ) -> ModelDeliverKeyResult:
            logger.info(
                "deliver key id=%s env_name=%s path=%s outcome=%s%s",
                request.name,
                env_name,
                home_relative_path,
                outcome.value,
                f" detail={detail}" if detail else "",
            )
            return ModelDeliverKeyResult(
                name=request.name,
                outcome=outcome,
                env_name=env_name,
                home_relative_path=home_relative_path,
                key_length=key_length,
                detail=detail,
                correlation_id=request.correlation_id,
            )

        delivery = self._deliveries.get(request.name)
        if delivery is None:
            return result(
                EnumDeliverKeyOutcome.NOT_DECLARED,
                detail=(
                    f"the contract declares no env file delivery {request.name!r}; "
                    "nothing was read and nothing was written"
                ),
            )
        declared = self._generated.get(delivery.key_id)
        if declared is None:
            return result(
                EnumDeliverKeyOutcome.NOT_DECLARED,
                env_name=delivery.env_name,
                home_relative_path=delivery.home_relative_path,
                detail=(
                    f"the delivery {request.name!r} names key_id "
                    f"{delivery.key_id!r}, which the contract's generated_keys "
                    "does not declare; nothing was read and nothing was written"
                ),
            )
        store_request = ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE,
            folder=declared.folder,
            key=declared.key,
            correlation_id=request.correlation_id,
        )
        handler = self._store_handler or HandlerSecretStore(
            overlay=self._overlay, onex_home=self._onex_home
        )
        store_result = await handler.handle(store_request)
        if (
            store_result.outcome is EnumSecretStoreOutcome.NOT_CONFIGURED
            and self._store_handler is None
            and self._overlay is None
        ):
            home = self._home or Path.home()
            overlay = host_env_overlay(home)
            if overlay is not None:
                store_result = await HandlerSecretStore(
                    overlay=overlay,
                    onex_home=self._onex_home,
                    bootstrap_store=IdentityFileStore(home / HOST_ENV_FILE),
                ).handle(store_request)
        value = store_result.value
        if store_result.outcome is not EnumSecretStoreOutcome.RESOLVED or value is None:
            detail = f"the store answered {store_result.outcome.value}"
            if store_result.detail:
                detail = f"{detail}: {store_result.detail}"
            return result(
                EnumDeliverKeyOutcome.KEY_UNREADABLE,
                env_name=delivery.env_name,
                home_relative_path=delivery.home_relative_path,
                detail=detail,
            )
        # The value exists only between these lines and inside the file.
        raw_value = value.get_secret_value()
        if not _SAFE_VALUE.match(raw_value):
            return result(
                EnumDeliverKeyOutcome.VALUE_UNSAFE,
                env_name=delivery.env_name,
                home_relative_path=delivery.home_relative_path,
                detail=(
                    "the stored value is not in the shape a systemd "
                    "EnvironmentFile= line parses without quoting; "
                    "nothing was written"
                ),
            )
        home = self._home or Path.home()
        target = home / delivery.home_relative_path
        content = f"{delivery.env_name}={raw_value}\n"
        key_length = len(raw_value)
        try:
            existing = target.read_bytes()
        except OSError:
            existing = None
        if existing == content.encode("utf-8"):
            try:
                mode_now = stat.S_IMODE(os.stat(target).st_mode)
                if mode_now != delivery.mode:
                    os.chmod(target, delivery.mode)
            except OSError as exc:
                return result(
                    EnumDeliverKeyOutcome.WRITE_FAILED,
                    env_name=delivery.env_name,
                    home_relative_path=delivery.home_relative_path,
                    detail=type(exc).__name__,
                )
            return result(
                EnumDeliverKeyOutcome.UNCHANGED,
                env_name=delivery.env_name,
                home_relative_path=delivery.home_relative_path,
                key_length=key_length,
                detail=(
                    "the file mode was corrected to the declared mode"
                    if mode_now != delivery.mode
                    else None
                ),
            )
        tmp = target.parent / f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, target)
            os.chmod(target, delivery.mode)
        except OSError as exc:
            with suppress(OSError):
                tmp.unlink(missing_ok=True)
            return result(
                EnumDeliverKeyOutcome.WRITE_FAILED,
                env_name=delivery.env_name,
                home_relative_path=delivery.home_relative_path,
                detail=type(exc).__name__,
            )
        return result(
            EnumDeliverKeyOutcome.DELIVERED,
            env_name=delivery.env_name,
            home_relative_path=delivery.home_relative_path,
            key_length=key_length,
        )


__all__ = ["HandlerDeliverKeyToEnvFile"]
