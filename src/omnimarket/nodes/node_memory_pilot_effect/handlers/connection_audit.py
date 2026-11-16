# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The connection record of a run: every socket connect outside loopback and the allowed hosts (bar B7)."""

from __future__ import annotations

import ipaddress
import socket
import sys
import threading
from typing import Any


class _AuditState:
    """Process-wide: an audit hook cannot be removed, so one hook reads this state."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.installed = False
        self.active = False
        self.allowed: frozenset[str] = frozenset()
        self.outside: set[str] = set()


_STATE = _AuditState()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event != "socket.connect" or not _STATE.active:
        return
    address = args[1] if len(args) > 1 else None
    if not isinstance(address, tuple) or not address:
        return  # a unix socket path
    host = str(address[0])
    if _is_loopback(host) or host in _STATE.allowed:
        return
    port = address[1] if len(address) > 1 else ""
    with _STATE.lock:
        _STATE.outside.add(f"{host}:{port}")


def _resolved(hosts: tuple[str, ...]) -> frozenset[str]:
    allowed: set[str] = set(hosts)
    for host in hosts:
        try:
            for info in socket.getaddrinfo(host, None):
                allowed.add(str(info[4][0]))
        except OSError:
            continue
    return frozenset(allowed)


class SocketConnectionAudit:
    """Records ``socket.connect`` events between ``start`` and ``stop`` through the interpreter's audit hook."""

    def start(self, allowed_hosts: tuple[str, ...]) -> None:
        allowed = _resolved(allowed_hosts)
        with _STATE.lock:
            if not _STATE.installed:
                sys.addaudithook(_hook)
                _STATE.installed = True
            _STATE.allowed = allowed
            _STATE.outside = set()
            _STATE.active = True

    def stop(self) -> tuple[str, ...]:
        with _STATE.lock:
            _STATE.active = False
            return tuple(sorted(_STATE.outside))
