# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The certificate check of the iLO setup verb (OMN-22870).

The pin is written only when the certificate the iLO PRESENTED on the connection agrees with what the
iLO REPORTS about its own HTTPS certificate over Redfish. Comparison is by serial number (required) and
by SHA-256 when the iLO reports one. Nothing else is trusted: a report with no serial refuses.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from cryptography import x509
from pydantic import JsonValue

from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    JsonObject,
    as_object,
)


@dataclass(frozen=True)
class PresentedCertificate:
    sha256: str
    serial: str
    subject: str
    not_after: str


@dataclass(frozen=True)
class ReportedCertificate:
    serial: str | None
    sha256: str | None
    subject: str | None
    not_after: str | None


def _hex(value: str) -> str:
    """Hex digits only, lower case, no leading zeros: the one form two serials are compared in."""
    return re.sub(r"[^0-9a-f]", "", value.lower()).lstrip("0") or "0"


def presented(der: bytes) -> PresentedCertificate:
    cert = x509.load_der_x509_certificate(der)
    return PresentedCertificate(
        sha256=hashlib.sha256(der).hexdigest(),
        serial=format(cert.serial_number, "x"),
        subject=cert.subject.rfc4514_string(),
        not_after=cert.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _find(body: JsonValue, predicate_key: str) -> JsonObject | None:
    """The first nested object that holds ``predicate_key``."""
    if isinstance(body, dict):
        if predicate_key in body:
            return body
        for value in body.values():
            found = _find(value, predicate_key)
            if found is not None:
                return found
    elif isinstance(body, list):
        for item in body:
            found = _find(item, predicate_key)
            if found is not None:
                return found
    return None


def _text(body: JsonObject, *names: str) -> str | None:
    lowered = {k.lower(): v for k, v in body.items()}
    for name in names:
        value = lowered.get(name.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def reported(body: JsonObject) -> ReportedCertificate:
    """What the iLO's HttpsCert resource says about its certificate."""
    info = as_object(_find(body, "SerialNumber"))
    fingerprint_holder = _find_fingerprint(body)
    fingerprint = None
    if fingerprint_holder is not None:
        for key, value in fingerprint_holder.items():
            if (
                isinstance(value, str)
                and ("fingerprint" in key.lower() or "thumbprint" in key.lower())
                and "sha1" not in key.lower()
            ):
                fingerprint = re.sub(r"[^0-9a-f]", "", value.lower())
                break
    return ReportedCertificate(
        serial=_text(info, "SerialNumber"),
        sha256=fingerprint if fingerprint and len(fingerprint) == 64 else None,
        subject=_text(info, "Subject"),
        not_after=_text(info, "ValidNotAfter", "NotAfter"),
    )


def _find_fingerprint(body: JsonValue) -> JsonObject | None:
    if isinstance(body, dict):
        for key in body:
            lowered = key.lower()
            if "fingerprint" in lowered or "thumbprint" in lowered:
                return body
        for value in body.values():
            found = _find_fingerprint(value)
            if found is not None:
                return found
    elif isinstance(body, list):
        for item in body:
            found = _find_fingerprint(item)
            if found is not None:
                return found
    return None


def matches(seen: PresentedCertificate, told: ReportedCertificate) -> bool:
    """True only when the iLO reported a serial and it, and any reported SHA-256, equal the presented ones."""
    if told.serial is None or _hex(told.serial) != _hex(seen.serial):
        return False
    return told.sha256 is None or told.sha256 == seen.sha256


def side_by_side(
    seen: PresentedCertificate, told: ReportedCertificate
) -> tuple[str, ...]:
    """Printable rows: what the connection presented, and what the iLO reports, in two columns."""
    absent = "(not reported by this iLO)"
    rows = (
        ("sha256", seen.sha256, told.sha256 or absent),
        ("serial", seen.serial, told.serial or absent),
        ("subject", seen.subject, told.subject or absent),
        ("not after", seen.not_after, told.not_after or absent),
    )
    lines = [f"  {'':10}{'presented on the connection':<68}reported by the iLO"]
    lines += [f"  {name:<10}{left:<68}{right}" for name, left, right in rows]
    return tuple(lines)
