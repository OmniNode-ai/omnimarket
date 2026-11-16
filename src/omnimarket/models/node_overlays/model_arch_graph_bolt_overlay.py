# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``arch_graph_bolt`` overlay: the architecture graph Bolt URI and the hosts that use it (OMN-24687).

``node_architecture_graph_query_effect`` and ``node_architecture_graph_populate_effect``
read the overlay file ``<overlay root>/arch_graph_bolt/overlay.yaml`` when
``ARCH_GRAPH_BOLT_URI`` is unset: the file declares one ``bolt_uri`` and a ``hosts`` list,
and a host whose short name is in the list gets that address. This model is what the
overlay home validates the document through, so it imports pydantic alone. It checks the
shape of the document and that the address is a Bolt URI with a host; which hosts appear
and where the service runs is the operator's choice, so no host name and no address lives
here.
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

SCHEMA_VERSION = "arch_graph_bolt.v1"

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_BOLT_SCHEMES = ("bolt", "bolt+s", "bolt+ssc")


def _bolt_scheme(value: str) -> str:
    """Accept ``<bolt scheme>://<host>[:<port>]`` and nothing after it."""
    if any(ch.isspace() for ch in value):
        raise ValueError("must not contain whitespace or trailing text")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"must be a valid Bolt URI ({exc})") from exc
    if parts.scheme not in _BOLT_SCHEMES:
        raise ValueError(f"must be a Bolt URI starting with one of {_BOLT_SCHEMES}")
    if not parts.hostname:
        raise ValueError("must name a host after the scheme")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("must be a bare host and optional port, no path or query")
    if port is not None and port == 0:
        raise ValueError("port must be nonzero")
    return value


BoltUri = Annotated[NonBlank, AfterValidator(_bolt_scheme)]


class ModelArchGraphBoltOverlay(BaseModel):
    """``arch_graph_bolt``: one Bolt URI, declared once, and the hosts that use it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["arch_graph_bolt.v1"]
    bolt_uri: BoltUri
    hosts: tuple[NonBlank, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_host_names(self) -> ModelArchGraphBoltOverlay:
        seen: set[str] = set()
        for host in self.hosts:
            key = host.casefold()
            if key in seen:
                raise ValueError(
                    f"two entries name the same host {host!r} ignoring case"
                )
            seen.add(key)
        return self


__all__ = [
    "SCHEMA_VERSION",
    "ModelArchGraphBoltOverlay",
]
