# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one rule for when a secret store may be reached over plain http (OMN-23193).

A store request carries the machine identity's login and a Bearer token, and a
create request carries the secret itself, so a plain-http address to a host the
overlay has not named sends them in clear text. Every secret store overlay and
every adapter that builds a request from one asks this module; none holds a
copy of the rule.

The rule: an https address is always accepted. A plain-http address is
accepted only when its host is a loopback name or address (the loopback names
come from ``configs/network_bindings.yaml``) or is named by the overlay's
``plain_http_hosts``. Each entry there is a host name, an IP address or a
network in CIDR form; a name matches case-insensitively, an address or network
matches an address host. The overlay's default is an empty list, so a machine
that names no host reaches only loopback over plain http.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
from collections.abc import Iterable

from omnimarket.handlers.network_bindings import network_host

_IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class PlainHttpRefusedError(ValueError):
    """A store address would send credentials in clear text to a host not allowed.

    Names the host and the overlay key, never a credential.
    """


def parse_plain_http_host(entry: str) -> _IPNetwork | str:
    """One ``plain_http_hosts`` entry as a network, or a lower-cased host name.

    Raises:
        ValueError: the entry is empty, carries a scheme, path or port, or
            holds a ``/`` that is not a valid network.
    """
    text = entry.strip()
    if not text or any(mark in text for mark in ("://", "@", " ")):
        raise ValueError(f"plain_http_hosts entry {entry!r} is not a host")
    if "/" in text:
        try:
            return ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise ValueError(
                f"plain_http_hosts entry {entry!r} is not a network"
            ) from None
    try:
        return ipaddress.ip_network(text)
    except ValueError:
        pass
    if ":" in text:
        raise ValueError(f"plain_http_hosts entry {entry!r} carries a port")
    return text.lower()


def _host_address(hostname: str) -> _IPAddress | None:
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


def plain_http_allowed(hostname: str | None, allowed: Iterable[str] = ()) -> bool:
    """Whether a plain-http request to ``hostname`` is allowed (see module doc)."""
    if hostname is None or not hostname:
        return False
    host = hostname.lower()
    address = _host_address(host)
    if host == network_host("loopback.name"):
        return True
    if address is not None and address.is_loopback:
        return True
    for entry in allowed:
        parsed = parse_plain_http_host(entry)
        if isinstance(parsed, str):
            if address is None and host == parsed:
                return True
        elif (
            address is not None
            and address.version == parsed.version
            and address in parsed
        ):
            return True
    return False


def require_safe_store_address(address: str, allowed: Iterable[str] = ()) -> None:
    """Refuse an address that is not https and not an allowed plain-http host.

    Raises:
        PlainHttpRefusedError: the scheme is http and the host is not allowed,
            or the scheme is neither http nor https.
    """
    parts = urllib.parse.urlsplit(address)
    if parts.scheme == "https":
        return
    if parts.scheme == "http":
        if plain_http_allowed(parts.hostname, allowed):
            return
        raise PlainHttpRefusedError(
            f"address: plain http is accepted only to a loopback host or a host "
            f"named in plain_http_hosts; host {parts.hostname!r} is neither, "
            "so a remote store needs https"
        )
    raise PlainHttpRefusedError("address: the scheme must be http or https")


def validate_plain_http_hosts(entries: Iterable[str]) -> tuple[str, ...]:
    """The entries unchanged when each parses; ``ValueError`` names the bad one."""
    checked = tuple(entries)
    for entry in checked:
        parse_plain_http_host(entry)
    return checked


__all__ = [
    "PlainHttpRefusedError",
    "parse_plain_http_host",
    "plain_http_allowed",
    "require_safe_store_address",
    "validate_plain_http_hosts",
]
