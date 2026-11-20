# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read local Tailscale status and keep only the data the expiry judge needs."""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.overlay_keys.model_overlay_documents import ModelLabHostsTable
from omnimarket.nodes.node_silent_failure_alarm_effect.host_table import event_label
from omnimarket.nodes.node_tailscale_key_expiry_alarm_effect.models import (
    ModelKeyExpiryReading,
)

_MAC_APP = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
_WATCHING_HOST = "watching-host"


def read_tailscale_status() -> dict[str, object] | None:
    executable = shutil.which("tailscale")
    if executable is None and _MAC_APP.is_file():
        executable = str(_MAC_APP)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "status", "--json"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        document = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        # The status may contain keys and identifying names. Keep no raw output,
        # including a failed subprocess's stdout or stderr.
        return None
    return document if isinstance(document, dict) else None


def _label(name: str) -> str:
    machine = name.rsplit("@", 1)[-1].strip().strip("[]")
    try:
        return str(ipaddress.ip_address(machine))
    except ValueError:
        return event_label(machine)


def host_aliases(
    table: ModelLabHostsTable, additional: Mapping[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    hosts = {row.name for row in table.hosts}
    if len(hosts) != len(table.hosts) or additional.keys() - hosts:
        raise ValueError(
            "duplicate lab host or additional alias for an unknown lab host"
        )
    return {
        row.name: (
            row.name,
            *row.hostnames,
            row.target,
            *row.fallback_targets,
            *additional.get(row.name, ()),
        )
        for row in table.hosts
    }


def _node_labels(node: Mapping[str, object]) -> set[str]:
    names = [node.get("HostName"), node.get("DNSName")]
    ips = node.get("TailscaleIPs")
    if isinstance(ips, list):
        names.extend(ips)
    return {_label(name) for name in names if isinstance(name, str) and name.strip()}


def _expiry(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None and parsed.year > 1:
            return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        pass
    return None


def read_status(
    document: Mapping[str, object] | None,
    aliases: Mapping[str, tuple[str, ...]],
    at: datetime,
    *,
    watching_host: str = _WATCHING_HOST,
) -> dict[str, ModelKeyExpiryReading] | None:
    """Project a running status to lab host IDs and a role for an unmatched Self."""
    if document is None or document.get("BackendState") != "Running":
        return None
    own = document.get("Self")
    peers = document.get("Peer")
    if not isinstance(own, dict) or (peers is not None and not isinstance(peers, dict)):
        return None
    names = {
        host: {_label(alias) for alias in values} for host, values in aliases.items()
    }
    readings: dict[str, ModelKeyExpiryReading] = {}
    ambiguous: set[str] = set()
    for node in (own, *(peers or {}).values()):
        if not isinstance(node, dict):
            continue
        labels = _node_labels(node)
        matches = [host for host, values in names.items() if labels & values]
        if len(matches) > 1:
            ambiguous.update(matches)
            continue
        if matches:
            subject = matches[0]
        elif node is own:
            subject = watching_host
        else:
            continue
        if subject in readings:
            ambiguous.add(subject)
        readings[subject] = ModelKeyExpiryReading(
            observed_at=at,
            expires_at=_expiry(node.get("KeyExpiry")),
            expired=node.get("Expired") is True,
        )
    return {host: row for host, row in readings.items() if host not in ambiguous}
