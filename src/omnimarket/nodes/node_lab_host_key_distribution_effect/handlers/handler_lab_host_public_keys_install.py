# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the declared public key set into authorized_keys on every available pool host."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
    not_configured_message,
)
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable

from ..models.model_lab_host_key_distribution import (
    EnumKeyFile,
    EnumLineState,
    public_key_parts,
)
from ..models.model_lab_host_public_keys_install import (
    PUBLIC_KEYS_OVERLAY_ID,
    EnumHostInstallStatus,
    ModelHostInstallResult,
    ModelLabHostPublicKeysInstallRequest,
    ModelLabHostPublicKeysInstallResult,
    ModelLabHostPublicKeysOverlay,
)
from ..protocols.protocol_host_key_transport import (
    HostTransportError,
    ProtocolHostKeyTransport,
)
from .adapter_ssh_host_key_transport import SshHostKeyTransport


def resolve_declared_keys(
    environ: Mapping[str, str] | None = None,
) -> ModelLabHostPublicKeysOverlay:
    """The declared key set: the ``lab_host_public_keys`` overlay under ``ONEX_SKILL_OVERLAY_ROOTS``."""
    document = load_structured_overlay(
        PUBLIC_KEYS_OVERLAY_ID,
        ModelLabHostPublicKeysOverlay,
        environ,
        missing_message=not_configured_message(
            PUBLIC_KEYS_OVERLAY_ID, f"{PUBLIC_KEYS_OVERLAY_ID}.keys"
        ),
    )
    return document


class HandlerLabHostPublicKeysInstall:
    """Ensure every declared key in each pool host's authorized_keys; a second run adds nothing."""

    def __init__(
        self,
        transport: ProtocolHostKeyTransport | None = None,
        table_source: Callable[[], ModelLabHostsTable] | None = None,
        keys_source: Callable[[], ModelLabHostPublicKeysOverlay] | None = None,
    ) -> None:
        self._transport = transport
        self._table_source = table_source or resolve_lab_hosts_table
        self._keys_source = keys_source or resolve_declared_keys

    def handle(
        self, request: ModelLabHostPublicKeysInstallRequest
    ) -> ModelLabHostPublicKeysInstallResult:
        try:
            if request.table is not None:
                table = request.table
            elif request.table_path is not None:
                table = ModelLabHostsTable.model_validate(
                    yaml.safe_load(Path(request.table_path).read_text(encoding="utf-8"))
                )
            else:
                table = self._table_source()
            declared = request.keys if request.keys is not None else self._keys_source()
            rows = self._scope(table, request.hosts)
        except (OSError, ValueError, yaml.YAMLError, NodeOverlayError) as exc:
            return ModelLabHostPublicKeysInstallResult(
                dry_run=request.dry_run, exit_code=2, refusal=str(exc)
            )
        transport = (
            self._transport
            if self._transport is not None
            else SshHostKeyTransport(
                local_host=request.local_host, timeout_s=request.timeout_s
            )
        )
        results = tuple(self._host(row, declared, request, transport) for row in rows)
        failed = any(r.status == EnumHostInstallStatus.REFUSED for r in results)
        return ModelLabHostPublicKeysInstallResult(
            dry_run=request.dry_run, hosts=results, exit_code=1 if failed else 0
        )

    @staticmethod
    def _scope(
        table: ModelLabHostsTable, names: tuple[str, ...]
    ) -> list[ModelLabHostRow]:
        by_name = {host.name: host for host in table.hosts}
        if not names:
            return list(table.hosts)
        unknown = [name for name in dict.fromkeys(names) if name not in by_name]
        if unknown:
            raise ValueError(f"unknown host {unknown[0]!r}")
        return [by_name[name] for name in dict.fromkeys(names)]

    def _host(
        self,
        host: ModelLabHostRow,
        declared: ModelLabHostPublicKeysOverlay,
        request: ModelLabHostPublicKeysInstallRequest,
        transport: ProtocolHostKeyTransport,
    ) -> ModelHostInstallResult:
        if not host.available:
            return ModelHostInstallResult(
                host=host.name,
                status=EnumHostInstallStatus.SKIPPED,
                detail=f"unavailable: {host.unavailable_reason or 'not available'}",
            )
        added = 0
        present = 0
        try:
            for key in declared.keys:
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
            return ModelHostInstallResult(
                host=host.name,
                status=EnumHostInstallStatus.REFUSED,
                keys_added=added,
                keys_present=present,
                detail=str(exc),
            )
        return ModelHostInstallResult(
            host=host.name,
            status=EnumHostInstallStatus.PLANNED
            if request.dry_run
            else EnumHostInstallStatus.OK,
            keys_added=added,
            keys_present=present,
        )
