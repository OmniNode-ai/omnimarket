# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install the public host keys the lab hosts declare into the selected hosts' known_hosts."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
)
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.lab_host_key import EnumKeyFile, EnumLineState
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable

from ..models.model_lab_host_declared_host_keys_install import (
    ModelLabHostDeclaredHostKeysInstallRequest,
)
from ..models.model_lab_host_public_keys_install import (
    EnumHostInstallStatus,
    ModelHostInstallResult,
    ModelLabHostPublicKeysInstallResult,
)
from ..protocols.protocol_host_key_transport import (
    HostTransportError,
    ProtocolHostKeyTransport,
)
from .adapter_ssh_host_key_transport import SshHostKeyTransport

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _patterns_for(peer: ModelLabHostRow) -> str:
    names = [
        peer.name,
        peer.target.rsplit("@", 1)[-1],
        *(target.rsplit("@", 1)[-1] for target in peer.fallback_targets),
    ]
    return ",".join(dict.fromkeys(name for name in names if _NAME_RE.fullmatch(name)))


class HandlerLabHostDeclaredHostKeysInstall:
    """Ensure every declared host key of every declaring row in each destination's known_hosts."""

    def __init__(
        self,
        transport: ProtocolHostKeyTransport | None = None,
        table_source: Callable[[], ModelLabHostsTable] | None = None,
    ) -> None:
        self._transport = transport
        self._table_source = table_source or resolve_lab_hosts_table

    def handle(
        self, request: ModelLabHostDeclaredHostKeysInstallRequest
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
            peers = [row for row in table.hosts if row.host_keys]
            if not peers:
                return ModelLabHostPublicKeysInstallResult(
                    dry_run=request.dry_run,
                    exit_code=2,
                    refusal="no lab host declares host_keys",
                )
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
        results = tuple(
            self._destination(row, peers, request, transport) for row in rows
        )
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

    def _destination(
        self,
        destination: ModelLabHostRow,
        peers: list[ModelLabHostRow],
        request: ModelLabHostDeclaredHostKeysInstallRequest,
        transport: ProtocolHostKeyTransport,
    ) -> ModelHostInstallResult:
        if not destination.available:
            return ModelHostInstallResult(
                host=destination.name,
                status=EnumHostInstallStatus.SKIPPED,
                detail=f"unavailable: {destination.unavailable_reason or 'not available'}",
            )
        added = 0
        present = 0
        try:
            for peer in peers:
                if peer.name == destination.name:
                    continue
                patterns = _patterns_for(peer)
                for key in peer.host_keys:
                    line = f"{patterns} {key}"
                    state = transport.ensure_line(
                        destination,
                        EnumKeyFile.KNOWN_HOSTS,
                        line,
                        line,
                        apply=not request.dry_run,
                    )
                    if state == EnumLineState.PRESENT:
                        present += 1
                    else:
                        added += 1
        except (HostTransportError, ValueError) as exc:
            return ModelHostInstallResult(
                host=destination.name,
                status=EnumHostInstallStatus.REFUSED,
                keys_added=added,
                keys_present=present,
                detail=str(exc),
            )
        return ModelHostInstallResult(
            host=destination.name,
            status=EnumHostInstallStatus.PLANNED
            if request.dry_run
            else EnumHostInstallStatus.OK,
            keys_added=added,
            keys_present=present,
        )
