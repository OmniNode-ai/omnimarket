# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool hosts' hook-event names, read from the host table the deployment names (OMN-22755).

A hook event's ``host`` is the machine's own name and the ledger names a pool host by id. A host
table declares both: each host id with the machine names it answers to. The alarm derives its
host aliases from that table instead of holding a second list, so a host added to the table is
attributed the day it is added. A table that cannot be read is an error the caller turns into an
unreadable event table, never into an empty alias set that passes.

The table is a YAML mapping ``hosts: {<host id>: {hostnames: [<name>, ...]}}``. Every other key
of a host is ignored; a host with no ``hostnames`` is not attributable and is left out.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table


class HostTableError(ValueError):
    """The host table could not be read as a table of hosts and their names."""


def event_label(hostname: str) -> str:
    """The lowercased first DNS label a hook event of ``hostname`` carries."""
    return hostname.strip().lower().split(".", 1)[0]


def read_host_aliases(path: Path) -> dict[str, tuple[str, ...]]:
    """Map each host id of the table at ``path`` to the event labels it answers to.

    Raises ``HostTableError`` when the file is unreadable, is not a table of hosts, or gives one
    label to two hosts (an event of that name could not be attributed to either).
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HostTableError(f"{path}: {' '.join(str(exc).split())[:200]}") from exc
    hosts = document.get("hosts") if isinstance(document, dict) else None
    if not isinstance(hosts, Mapping) or not hosts:
        raise HostTableError(f"{path}: no hosts mapping")
    aliases: dict[str, tuple[str, ...]] = {}
    owner: dict[str, str] = {}
    for host, declared in hosts.items():
        names = declared.get("hostnames") if isinstance(declared, Mapping) else None
        if not names:
            continue
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise HostTableError(f"{path}: hostnames of {host} is not a list of names")
        labels = tuple(dict.fromkeys(event_label(n) for n in names if n.strip()))
        for label in labels:
            if owner.setdefault(label, str(host)) != str(host):
                raise HostTableError(
                    f"{path}: the event name {label} belongs to {owner[label]} and {host}"
                )
        if labels:
            aliases[str(host)] = labels
    if not aliases:
        raise HostTableError(f"{path}: no host declares hostnames")
    return aliases


def read_unavailable_hosts(
    environ: Mapping[str, str] | None = None,
) -> tuple[frozenset[str], str]:
    """The host ids the lab host table declares unavailable, and why it could not be read ('' when read).

    A host with ``available: false`` takes no lane (OMN-22778): lane rows that name it are not lane work
    that lost its hook events. The table is the one lab host list, read the way every other consumer reads
    it (``resolve_lab_hosts_table``). A table that cannot be read gives an empty set and the reason, so the
    caller keeps counting every host and says so; it never hides a host on a guess.
    """
    env = os.environ if environ is None else environ
    try:
        table = resolve_lab_hosts_table(env)
    except (
        Exception
    ) as exc:  # any unreadable table is reported, never raised into the tick
        return frozenset(), " ".join(f"{type(exc).__name__}: {exc}".split())[:200]
    return frozenset(row.name for row in table.hosts if not row.available), ""
