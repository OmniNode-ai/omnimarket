# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the overlay-declared public keys into authorized_keys on every pool host."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_node_overlay,
)
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable

from ..models.model_lab_host_key_distribution import (
    EnumInstallStatus,
    EnumKeyFile,
    EnumLineState,
    ModelDeclaredKeysInstallRequest,
    ModelDeclaredKeysInstallResult,
    ModelHostInstallReceipt,
    public_key_parts,
)
from ..protocols.protocol_host_key_transport import (
    HostTransportError,
    ProtocolHostKeyTransport,
)
from .adapter_ssh_host_key_transport import SshHostKeyTransport

NODE_OVERLAY_NAME = "node_lab_host_key_distribution_effect"
KEYS_FIELD = "declared_public_keys"


def resolve_declared_keys(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The declared public keys, one per line of the overlay's ``declared_public_keys``."""
    fields = load_node_overlay(
        NODE_OVERLAY_NAME, fields=frozenset({KEYS_FIELD}), environ=environ
    )
    text = fields.get(KEYS_FIELD)
    if text is None:
        raise NodeOverlayError(
            f"{KEYS_FIELD} is not configured; supply an overlay with field "
            f"`{KEYS_FIELD}` at <root>/{NODE_OVERLAY_NAME}/overlay.yaml under a root "
            "named in ONEX_SKILL_OVERLAY_ROOTS"
        )
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        public_key_parts(line)
    return tuple(lines)


class HandlerLabHostPublicKeysInstall:
    """Ensure each declared key in each pool host's authorized_keys; one receipt per host."""

    def __init__(
        self,
        transport: ProtocolHostKeyTransport | None = None,
        table_source: Callable[[], ModelLabHostsTable] | None = None,
        key_source: Callable[[], tuple[str, ...]] | None = None,
    ) -> None:
        self._transport = transport
        self._table_source = table_source or resolve_lab_hosts_table
        self._key_source = key_source or resolve_declared_keys

    def handle(
        self, request: ModelDeclaredKeysInstallRequest
    ) -> ModelDeclaredKeysInstallResult:
        try:
            if request.table is not None:
                table = request.table
            elif request.table_path is not None:
                table = ModelLabHostsTable.model_validate(
                    yaml.safe_load(Path(request.table_path).read_text(encoding="utf-8"))
                )
            else:
                table = self._table_source()
            keys = (
                request.public_keys
                if request.public_keys is not None
                else self._key_source()
            )
            if not keys:
                raise ValueError("no declared public keys")
            selected = set(request.hosts) if request.hosts is not None else None
            if selected is not None:
                unknown = selected - {h.name for h in table.hosts}
                if unknown:
                    raise ValueError(f"unknown host {sorted(unknown)[0]!r}")
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return ModelDeclaredKeysInstallResult(
                dry_run=request.dry_run, exit_code=2, refusal=str(exc)
            )

        transport = self._transport or SshHostKeyTransport(
            request.local_host, request.timeout_s
        )
        receipts = tuple(
            self._host(transport, host, keys, request)
            for host in table.hosts
            if selected is None or host.name in selected
        )
        failed = any(r.status == EnumInstallStatus.REFUSED for r in receipts)
        return ModelDeclaredKeysInstallResult(
            dry_run=request.dry_run, receipts=receipts, exit_code=1 if failed else 0
        )

    def _host(
        self,
        transport: ProtocolHostKeyTransport,
        host: ModelLabHostRow,
        keys: tuple[str, ...],
        request: ModelDeclaredKeysInstallRequest,
    ) -> ModelHostInstallReceipt:
        if not host.available:
            return ModelHostInstallReceipt(
                host=host.name,
                status=EnumInstallStatus.SKIPPED,
                detail=f"unavailable: {host.unavailable_reason or 'not available'}",
            )
        if host.contributor and request.hosts is None:
            return ModelHostInstallReceipt(
                host=host.name,
                status=EnumInstallStatus.SKIPPED,
                detail="contributor machine; name it to install",
            )
        added = present = 0
        try:
            for key in keys:
                state = transport.ensure_line(
                    host,
                    EnumKeyFile.AUTHORIZED_KEYS,
                    public_key_parts(key),
                    key,
                    apply=not request.dry_run,
                )
                if state == EnumLineState.PRESENT:
                    present += 1
                else:
                    added += 1
        except (HostTransportError, ValueError) as exc:
            return ModelHostInstallReceipt(
                host=host.name,
                status=EnumInstallStatus.REFUSED,
                keys_added=added,
                keys_present=present,
                detail=str(exc),
            )
        return ModelHostInstallReceipt(
            host=host.name,
            status=EnumInstallStatus.PLANNED
            if request.dry_run
            else EnumInstallStatus.OK,
            keys_added=added,
            keys_present=present,
        )
