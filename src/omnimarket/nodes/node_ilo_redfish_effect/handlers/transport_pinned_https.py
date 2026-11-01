# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pinned-certificate HTTPS transport for iLO 4 (OMN-21835).

iLO presents a self-signed certificate by default. This transport never turns certificate verification
off. The TLS context requires verification, trusts exactly one certificate (the host's pinned PEM,
which must hash to the overlay's SHA-256 pin), and demands TLS 1.2 or newer. The system trust store is
not loaded. After the handshake the presented certificate's SHA-256 is compared with the pin again.

Host-name matching is the one check left out, because the iLO certificate names the iLO's own DNS name
while the client connects by address; the pin replaces it. Any TLS failure is an error code. The
transport never retries with a weaker setting.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
from pathlib import Path
from typing import Literal

from pydantic import JsonValue, SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ModelRedfishResponse,
    RedfishTransportError,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
)

_MAX_BODY_BYTES = 4 * 1024 * 1024
API_PREFIX = "/redfish/v1"


def pinned_context(host: ModelIloHost, overlay_dir: Path) -> ssl.SSLContext:
    """A verifying TLS context that trusts only the host's pinned certificate.

    Raises:
        RedfishTransportError: kind ``tls`` when the PEM is unreadable, is not a certificate, or does
            not hash to the overlay pin.
    """
    root = overlay_dir.resolve()
    pem_path = (root / host.tls.ca_file).resolve()
    if not pem_path.is_relative_to(root) or not pem_path.is_file():
        raise RedfishTransportError("tls", request_sent=False)
    try:
        pem_text = pem_path.read_text(encoding="ascii")
        der = ssl.PEM_cert_to_DER_cert(pem_text.strip())
    except (OSError, UnicodeError, ValueError):
        raise RedfishTransportError("tls", request_sent=False) from None
    if hashlib.sha256(der).hexdigest() != host.tls.sha256:
        raise RedfishTransportError("tls", request_sent=False)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    # Host-name matching is replaced by the pin (see the module docstring); the chain is still verified.
    context.check_hostname = False
    try:
        context.load_verify_locations(cadata=pem_text)
    except ssl.SSLError:
        raise RedfishTransportError("tls", request_sent=False) from None
    return context


class PinnedHttpsTransport:
    """One request per connection to one iLO, over a pinned context."""

    def __init__(
        self, host: ModelIloHost, context: ssl.SSLContext, *, timeout_s: float = 30.0
    ) -> None:
        self._host = host
        self._context = context
        self._timeout = timeout_s

    def request(
        self,
        method: Literal["GET", "POST", "PATCH", "DELETE"],
        path: str,
        *,
        body: JsonValue = None,
        token: SecretStr | None = None,
    ) -> ModelRedfishResponse:
        if (
            not path.startswith(API_PREFIX)
            or "://" in path
            or "\r" in path
            or "\n" in path
        ):
            raise RedfishTransportError("protocol", request_sent=False)
        headers = {"Accept": "application/json", "OData-Version": "4.0"}
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            headers["X-Auth-Token"] = token.get_secret_value()
        connection = http.client.HTTPSConnection(
            self._host.address,
            self._host.port,
            timeout=self._timeout,
            context=self._context,
        )
        sent = False
        try:
            try:
                connection.connect()
            except ssl.SSLError:
                raise RedfishTransportError("tls", request_sent=False) from None
            except TimeoutError:
                raise RedfishTransportError("timeout", request_sent=False) from None
            except OSError:
                raise RedfishTransportError("connect", request_sent=False) from None
            self._check_pin(connection)
            sent = True
            try:
                connection.request(method, path, body=payload, headers=headers)
                response = connection.getresponse()
                raw = response.read(_MAX_BODY_BYTES + 1)
            except TimeoutError:
                raise RedfishTransportError("timeout", request_sent=sent) from None
            except (OSError, http.client.HTTPException):
                raise RedfishTransportError("connect", request_sent=sent) from None
            if len(raw) > _MAX_BODY_BYTES:
                raise RedfishTransportError("protocol", request_sent=sent)
            parsed: JsonValue = None
            if raw:
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
            return ModelRedfishResponse(
                status=response.status,
                headers={k.lower(): v for k, v in response.getheaders()},
                body=parsed,
            )
        finally:
            connection.close()

    def _check_pin(self, connection: http.client.HTTPSConnection) -> None:
        sock = connection.sock
        if not isinstance(sock, ssl.SSLSocket):
            raise RedfishTransportError("tls", request_sent=False)
        der = sock.getpeercert(binary_form=True)
        if der is None or hashlib.sha256(der).hexdigest() != self._host.tls.sha256:
            raise RedfishTransportError("tls", request_sent=False)


def build_pinned_transport(
    host: ModelIloHost, overlay_dir: Path, *, timeout_s: float = 30.0
) -> PinnedHttpsTransport:
    """The production transport factory body: load and check the pin, then build the transport."""
    return PinnedHttpsTransport(
        host, pinned_context(host, overlay_dir), timeout_s=timeout_s
    )
