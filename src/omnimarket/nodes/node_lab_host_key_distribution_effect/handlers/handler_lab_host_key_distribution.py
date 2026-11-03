# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Distribute public user and host keys among explicitly scoped lab hosts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable

from ..models.model_lab_host_key_distribution import (
    EnumKeyFile,
    EnumLineState,
    EnumPairStatus,
    ModelHostKeyMaterial,
    ModelLabHostKeyDistributionRequest,
    ModelLabHostKeyDistributionResult,
    ModelPairResult,
    public_key_parts,
)
from ..protocols.protocol_host_key_transport import (
    HostTransportError,
    ProtocolHostKeyTransport,
)
from .adapter_ssh_host_key_transport import SshHostKeyTransport


class HandlerLabHostKeyDistribution:
    """Read each selected host once, then ensure both files for each ordered pair."""

    def __init__(
        self,
        transport: ProtocolHostKeyTransport | None = None,
        table_source: Callable[[], ModelLabHostsTable] | None = None,
    ) -> None:
        self._transport = transport if transport is not None else SshHostKeyTransport()
        self._table_source = table_source or resolve_lab_hosts_table

    def handle(
        self, request: ModelLabHostKeyDistributionRequest
    ) -> ModelLabHostKeyDistributionResult:
        try:
            if request.table is not None:
                table = request.table
            elif request.table_path is not None:
                table = ModelLabHostsTable.model_validate(
                    yaml.safe_load(Path(request.table_path).read_text(encoding="utf-8"))
                )
            else:
                table = self._table_source()
            by_name = {host.name: host for host in table.hosts}
            hosts: list[ModelLabHostRow] = []
            for name in dict.fromkeys(request.hosts):
                if name not in by_name:
                    raise ValueError(f"unknown host {name!r}")
                host = by_name[name]
                if not host.available:
                    raise ValueError(
                        f"unavailable host {name!r}: {host.unavailable_reason or 'not available'}"
                    )
                hosts.append(host)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            return ModelLabHostKeyDistributionResult(
                dry_run=request.dry_run, exit_code=2, refusal=str(exc)
            )

        material: dict[str, ModelHostKeyMaterial] = {}
        errors: dict[str, str] = {}
        for host in hosts:
            try:
                material[host.name] = self._transport.read_material(host, request)
            except HostTransportError as exc:
                errors[host.name] = str(exc)

        pairs = tuple(
            self._pair(source, destination, request, material, errors)
            for source in hosts
            for destination in hosts
            if source.name != destination.name
        )
        return ModelLabHostKeyDistributionResult(
            dry_run=request.dry_run,
            pairs=pairs,
            exit_code=1
            if errors or any(p.status == EnumPairStatus.REFUSED for p in pairs)
            else 0,
        )

    def _pair(
        self,
        source: ModelLabHostRow,
        destination: ModelLabHostRow,
        request: ModelLabHostKeyDistributionRequest,
        material: dict[str, ModelHostKeyMaterial],
        errors: dict[str, str],
    ) -> ModelPairResult:
        authorized_added = False
        known_added = False
        detail = "; ".join(
            f"{name}: {errors[name]}"
            for name in (source.name, destination.name)
            if name in errors
        )
        status = EnumPairStatus.PLANNED if request.dry_run else EnumPairStatus.OK
        try:
            if detail:
                raise HostTransportError(detail)
            user_key = material[source.name].user_key
            state = self._transport.ensure_line(
                destination,
                EnumKeyFile.AUTHORIZED_KEYS,
                public_key_parts(user_key),
                user_key,
                apply=not request.dry_run,
            )
            authorized_added = state != EnumLineState.PRESENT
            patterns = ",".join(
                dict.fromkeys((destination.name, destination.target.rsplit("@", 1)[-1]))
            )
            for key in material[destination.name].host_keys:
                line = f"{patterns} {public_key_parts(key)}"
                state = self._transport.ensure_line(
                    source,
                    EnumKeyFile.KNOWN_HOSTS,
                    line,
                    line,
                    apply=not request.dry_run,
                )
                known_added |= state != EnumLineState.PRESENT
        except (HostTransportError, ValueError) as exc:
            status = EnumPairStatus.REFUSED
            detail = str(exc)
        return ModelPairResult(
            source=source.name,
            destination=destination.name,
            status=status,
            authorized_key_added=authorized_added,
            known_hosts_added=known_added,
            detail=detail,
        )
