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
from collections.abc import Iterator
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import JsonValue

from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    JsonObject,
    api_path,
    as_object,
)


@dataclass(frozen=True)
class PresentedCertificate:
    sha256: str
    serial: str
    subject: str
    not_after: str
    # RSA modulus size in bits; None for a key that is not RSA (an EC key is never "too weak" here).
    rsa_bits: int | None = None
    self_signed: bool = True


# The smallest RSA key the setup accepts for an iLO certificate. Anything below it is replaced.
MIN_RSA_BITS = 2048


@dataclass(frozen=True)
class ReportedCertificate:
    serial: str | None
    # Every SHA-256 the iLO reports; empty when it reports none.
    sha256: tuple[str, ...]
    subject: str | None
    not_after: str | None
    # True when the iLO names a SHA-256 it reports but the value cannot be read as one.
    sha256_invalid: bool = False


def is_weak(seen: PresentedCertificate) -> bool:
    """True when the certificate's RSA key is smaller than ``MIN_RSA_BITS``."""
    return seen.rsa_bits is not None and seen.rsa_bits < MIN_RSA_BITS


def colon_fingerprint(sha256: str) -> str:
    """``AB:CD:...`` upper-case, the form an iLO's own certificate page and the operator's notes use."""
    return ":".join(sha256[i : i + 2] for i in range(0, len(sha256), 2)).upper()


def _hex(value: str) -> str:
    """Hex digits only, lower case, no leading zeros: the one form two serials are compared in."""
    return re.sub(r"[^0-9a-f]", "", value.lower()).lstrip("0") or "0"


def presented(der: bytes) -> PresentedCertificate:
    cert = x509.load_der_x509_certificate(der)
    key = cert.public_key()
    return PresentedCertificate(
        rsa_bits=key.key_size if isinstance(key, rsa.RSAPublicKey) else None,
        self_signed=cert.issuer == cert.subject,
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


_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_FINGERPRINT_SEPARATORS = re.compile(r"[\s:\-]")


def _fingerprint_fields(body: JsonValue) -> Iterator[tuple[str, JsonValue]]:
    """Every field anywhere in the body whose name says fingerprint or thumbprint."""
    if isinstance(body, dict):
        for key, value in body.items():
            lowered = key.lower()
            if "fingerprint" in lowered or "thumbprint" in lowered:
                yield lowered, value
            else:
                yield from _fingerprint_fields(value)
    elif isinstance(body, list):
        for item in body:
            yield from _fingerprint_fields(item)


def _sha256_evidence(body: JsonObject) -> tuple[tuple[str, ...], bool]:
    """(every SHA-256 the iLO reports, True when it reports one that cannot be read).

    A field named for SHA-256 is SHA-256 evidence whatever its value; an unnamed fingerprint counts only
    when it is 64 hex digits; a SHA-1 field or any other length is never SHA-256 evidence. No field
    hides another: a SHA-256 that is present and wrong must never be taken for one that is absent.
    """
    found: list[str] = []
    invalid = False
    for key, value in _fingerprint_fields(body):
        if "sha1" in key or "sha-1" in key:
            continue
        explicit = "sha256" in key or "sha-256" in key
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        digits = (
            _FINGERPRINT_SEPARATORS.sub("", value).lower()
            if isinstance(value, str)
            else ""
        )
        if _SHA256_HEX.fullmatch(digits):
            found.append(digits)
        elif explicit:
            invalid = True
    return tuple(dict.fromkeys(found)), invalid


def reported(body: JsonObject) -> ReportedCertificate:
    """What the iLO's HttpsCert resource says about its certificate."""
    info = as_object(_find(body, "SerialNumber"))
    sha256, invalid = _sha256_evidence(body)
    return ReportedCertificate(
        serial=_text(info, "SerialNumber"),
        sha256=sha256,
        subject=_text(info, "Subject"),
        not_after=_text(info, "ValidNotAfter", "NotAfter"),
        sha256_invalid=invalid,
    )


def matches(seen: PresentedCertificate, told: ReportedCertificate) -> bool:
    """True only when the iLO reported a serial and it, and every reported SHA-256, equal the presented ones.

    A SHA-256 the iLO reported but that cannot be read never matches; only an absent one is optional.
    """
    if told.serial is None or _hex(told.serial) != _hex(seen.serial):
        return False
    return not told.sha256_invalid and all(
        value == seen.sha256 for value in told.sha256
    )


def side_by_side(
    seen: PresentedCertificate, told: ReportedCertificate
) -> tuple[str, ...]:
    """Printable rows: what the connection presented, and what the iLO reports, in two columns."""
    absent = "(not reported by this iLO)"
    if told.sha256_invalid:
        absent = "(reported, but not a readable SHA-256)"
    rows = (
        ("sha256", seen.sha256, ", ".join(told.sha256) or absent),
        ("serial", seen.serial, told.serial or absent),
        ("subject", seen.subject, told.subject or absent),
        ("not after", seen.not_after, told.not_after or absent),
    )
    lines = [f"  {'':10}{'presented on the connection':<68}reported by the iLO"]
    lines += [f"  {name:<10}{left:<68}{right}" for name, left, right in rows]
    return tuple(lines)


@dataclass(frozen=True)
class HttpsCertAction:
    """One action the iLO's HttpsCert resource names for itself, found and never assumed."""

    target: str
    # True when the iLO lists it in the older ``AvailableActions`` form, where the action is named in
    # the request body ("Action": name) and posted to the resource itself.
    named_in_body: bool

    def body(self, name: str, parameters: JsonObject) -> JsonObject:
        return (
            {"Action": name, **parameters} if self.named_in_body else dict(parameters)
        )


def _action_key(key: str) -> str:
    """``#HpeHttpsCert.GenerateCSR`` and ``GenerateCSR`` both give ``generatecsr``."""
    return key.lstrip("#").rpartition(".")[2].lower()


def find_action(
    body: JsonValue, resource_path: str, name: str
) -> HttpsCertAction | None:
    """The action ``name`` in the resource's own ``Actions`` object or ``AvailableActions`` list."""
    wanted = name.lower()
    stack: list[JsonValue] = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() == "actions" and isinstance(value, dict):
                    for action_key, entry in value.items():
                        if _action_key(action_key) == wanted and isinstance(
                            entry, dict
                        ):
                            target = entry.get("target") or entry.get("@odata.id")
                            path = api_path(target) if isinstance(target, str) else None
                            if path is not None:
                                return HttpsCertAction(target=path, named_in_body=False)
                elif key.lower() == "availableactions" and isinstance(value, list):
                    for entry in value:
                        listed = as_object(entry).get("Action")
                        if isinstance(listed, str) and listed.lower() == wanted:
                            return HttpsCertAction(
                                target=resource_path, named_in_body=True
                            )
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def action_names(body: JsonValue) -> tuple[str, ...]:
    """Every action name the resource lists, for a refusal detail. Names only."""
    found: list[str] = []
    stack: list[JsonValue] = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() == "actions" and isinstance(value, dict):
                    found.extend(_action_key(k) for k in value)
                elif key.lower() == "availableactions" and isinstance(value, list):
                    for entry in value:
                        listed = as_object(entry).get("Action")
                        if isinstance(listed, str):
                            found.append(listed.lower())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return tuple(sorted(set(found)))


def signing_request_of(body: JsonValue) -> str | None:
    """The PEM signing request an HttpsCert body (or a GenerateCSR answer) carries, if it carries one."""
    holder = _find(body, "CertificateSigningRequest")
    value = _text(holder, "CertificateSigningRequest") if holder is not None else None
    if value is not None and "BEGIN CERTIFICATE REQUEST" in value:
        return value
    return None
