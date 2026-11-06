# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Create the principal's ssh key and install it, forced to the gate, on every available pool host.

This is the one command the operator runs for the ssh credential. It makes an ed25519 key at the
overlay's ``key_path`` when none is there (the private half stays in that file and is never read
or printed), builds the authorized_keys line ``restrict,command="<gate>" <type> <blob> <comment>``
and has the lab host key transport ensure it on each host. ``restrict`` turns off the terminal,
forwarding and every other ssh feature, and ``command`` makes sshd run the gate whatever the client
asks. A second run finds the key and the line and changes nothing; ``dry_run`` writes nothing.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.models.overlay_keys import ModelLabHostRow, ModelLabHostsTable
from omnimarket.nodes.node_lab_host_key_distribution_effect.handlers.adapter_ssh_host_key_transport import (
    SshHostKeyTransport,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.models.model_lab_host_key_distribution import (
    EnumKeyFile,
    EnumLineState,
    public_key_parts,
)
from omnimarket.nodes.node_lab_host_key_distribution_effect.protocols.protocol_host_key_transport import (
    HostTransportError,
    ProtocolHostKeyTransport,
)

from ..models.model_read_only_principal import (
    EnumKeyInstallStatus,
    ModelKeyInstallHost,
    ModelKeyInstallRequest,
    ModelKeyInstallResult,
    ModelReadOnlyPrincipalOverlay,
)
from .handler_read_only_command_gate import load_overlay


def keygen_ed25519(path: Path, comment: str) -> None:
    """Make an ed25519 key with no passphrase at ``path``; ssh-keygen sets the private file to mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    done = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if done.returncode != 0:
        raise OSError("ssh-keygen did not make the key")


def authorized_line(gate_command: str, public_key: str, comment: str) -> str:
    """The forced-command authorized_keys line for ``public_key`` (its comment is replaced)."""
    return f'restrict,command="{gate_command}" {public_key_parts(public_key)} {comment}'


class HandlerReadOnlyPrincipalKeyInstall:
    """Operation ``install_key``: ensure the key and its forced-command line on each pool host."""

    def __init__(
        self,
        transport: ProtocolHostKeyTransport | None = None,
        table_source: Callable[[], ModelLabHostsTable] | None = None,
        overlay_source: Callable[[], ModelReadOnlyPrincipalOverlay] | None = None,
        keygen: Callable[[Path, str], None] = keygen_ed25519,
    ) -> None:
        self._transport = transport
        self._table_source = table_source or resolve_lab_hosts_table
        self._overlay_source = overlay_source or load_overlay
        self._keygen = keygen

    def handle(self, request: ModelKeyInstallRequest) -> ModelKeyInstallResult:
        try:
            overlay = (
                request.overlay
                if request.overlay is not None
                else self._overlay_source()
            )
            table = self._table_source()
            rows = self._scope(table, request.hosts)
        except (OSError, ValueError, NodeOverlayError) as exc:
            return ModelKeyInstallResult(
                dry_run=request.dry_run,
                key_created=False,
                key_present=False,
                exit_code=2,
                refusal=str(exc),
            )
        key = Path(overlay.ssh.key_path).expanduser()
        pub = Path(f"{key}.pub")
        created = False
        if not (key.is_file() and pub.is_file()):
            if not request.dry_run:
                try:
                    self._keygen(key, overlay.ssh.key_comment)
                except OSError as exc:
                    return ModelKeyInstallResult(
                        dry_run=False,
                        key_created=False,
                        key_present=False,
                        exit_code=2,
                        refusal=str(exc),
                    )
            created = True
        if request.dry_run and created:
            return ModelKeyInstallResult(
                dry_run=True,
                key_created=True,
                key_present=False,
                hosts=tuple(
                    ModelKeyInstallHost(
                        host=row.name,
                        status=EnumKeyInstallStatus.PLANNED
                        if row.available
                        else EnumKeyInstallStatus.SKIPPED,
                        detail="" if row.available else "unavailable",
                    )
                    for row in rows
                ),
                exit_code=0,
            )
        try:
            public_key = pub.read_text(encoding="utf-8").strip()
            line = authorized_line(
                overlay.ssh.gate_command, public_key, overlay.ssh.key_comment
            )
            needle = public_key_parts(public_key)
        except (OSError, ValueError) as exc:
            return ModelKeyInstallResult(
                dry_run=request.dry_run,
                key_created=created,
                key_present=pub.is_file(),
                exit_code=2,
                refusal=f"public key unreadable: {type(exc).__name__}",
            )
        transport = (
            self._transport
            if self._transport is not None
            else SshHostKeyTransport(
                local_host=request.local_host, timeout_s=request.timeout_s
            )
        )
        hosts = tuple(
            self._host(row, transport, needle, line, request.dry_run) for row in rows
        )
        failed = any(h.status == EnumKeyInstallStatus.REFUSED for h in hosts)
        return ModelKeyInstallResult(
            dry_run=request.dry_run,
            key_created=created,
            key_present=True,
            hosts=hosts,
            exit_code=1 if failed else 0,
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

    @staticmethod
    def _host(
        row: ModelLabHostRow,
        transport: ProtocolHostKeyTransport,
        needle: str,
        line: str,
        dry_run: bool,
    ) -> ModelKeyInstallHost:
        if not row.available:
            return ModelKeyInstallHost(
                host=row.name,
                status=EnumKeyInstallStatus.SKIPPED,
                detail=f"unavailable: {row.unavailable_reason or 'not available'}",
            )
        try:
            state = transport.ensure_line(
                row, EnumKeyFile.AUTHORIZED_KEYS, needle, line, apply=not dry_run
            )
        except (HostTransportError, ValueError) as exc:
            return ModelKeyInstallHost(
                host=row.name, status=EnumKeyInstallStatus.REFUSED, detail=str(exc)
            )
        if state == EnumLineState.PRESENT:
            return ModelKeyInstallHost(
                host=row.name, status=EnumKeyInstallStatus.OK, detail="already-present"
            )
        status = EnumKeyInstallStatus.PLANNED if dry_run else EnumKeyInstallStatus.OK
        return ModelKeyInstallHost(
            host=row.name, status=status, detail="would-add" if dry_run else "added"
        )
