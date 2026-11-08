# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab certificate authority of the iLO setup verb (OMN-22870).

An iLO 4 on firmware 2.82 presents an RSA 1024-bit certificate that a modern client refuses. The setup
replaces it: the iLO generates a 2048-bit signing request, this lab CA signs it, and the signed
certificate is imported. The CA key is created once and lives ONLY in the lab secret store, as one
value holding the CA certificate and its key (one value, so the store can never hold half a CA). The
CA certificate alone is public and is saved beside the overlay so that a connection can verify the
chain.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_certificate import (
    MIN_RSA_BITS,
)

LAB_CA_STORE_KEY = "ILO_LAB_CA_BUNDLE_PEM"
LAB_CA_FILE = "ilo-lab-ca.pem"
_CA_COMMON_NAME = "OmniNode lab iLO CA"
_CA_DAYS = 3650
_LEAF_DAYS = 1825
_CA_BITS = 3072


class LabCaError(Exception):
    """The CA or a signing request could not be used. The text names no key material."""


@dataclass(frozen=True)
class LabCa:
    certificate: x509.Certificate
    # The CA key never leaves this object except through ``bundle``, which only the store port sees.
    key: rsa.RSAPrivateKey

    @property
    def certificate_pem(self) -> str:
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")

    def bundle(self) -> SecretStr:
        """The one store value: the CA certificate PEM followed by the CA key PEM."""
        key_pem = self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        return SecretStr(self.certificate_pem + key_pem)


def create_lab_ca(now: dt.datetime) -> LabCa:
    """A new lab CA: RSA 3072, SHA-256, ten years, a CA that may sign leaf certificates only."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=_CA_BITS)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OmniNode"),
            x509.NameAttribute(NameOID.COMMON_NAME, _CA_COMMON_NAME),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=_CA_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return LabCa(certificate=certificate, key=key)


def load_lab_ca(bundle: SecretStr) -> LabCa:
    """The CA from its store value. Raises ``LabCaError`` for a value that is not a certificate and key."""
    raw = bundle.get_secret_value().encode("ascii", errors="ignore")
    try:
        certificate = x509.load_pem_x509_certificate(raw)
        key = serialization.load_pem_private_key(
            raw[raw.index(b"-----BEGIN PRIVATE") :], None
        )
    except ValueError:
        raise LabCaError("the stored lab CA is not a certificate and a key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise LabCaError("the stored lab CA key is not RSA")
    if key.public_key().public_numbers() != _rsa_public(certificate):
        raise LabCaError("the stored lab CA key does not belong to its certificate")
    return LabCa(certificate=certificate, key=key)


def _rsa_public(certificate: x509.Certificate) -> rsa.RSAPublicNumbers | None:
    public = certificate.public_key()
    return public.public_numbers() if isinstance(public, rsa.RSAPublicKey) else None


def read_csr(pem: str) -> x509.CertificateSigningRequest:
    """A signing request the iLO generated, accepted only when it is signed and carries RSA >= 2048."""
    try:
        csr = x509.load_pem_x509_csr(pem.encode("ascii"))
    except (ValueError, UnicodeError):
        raise LabCaError("the iLO's signing request is not a PEM request") from None
    if not csr.is_signature_valid:
        raise LabCaError(
            "the iLO's signing request does not verify against its own key"
        )
    public = csr.public_key()
    if not isinstance(public, rsa.RSAPublicKey) or public.key_size < MIN_RSA_BITS:
        raise LabCaError(
            f"the iLO's signing request key is not RSA of at least {MIN_RSA_BITS} bits"
        )
    return csr


def sign_csr(
    ca: LabCa, csr: x509.CertificateSigningRequest, *, address: str, now: dt.datetime
) -> x509.Certificate:
    """A server certificate for the request's key, naming the iLO's address, valid five years."""
    try:
        san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(address))
    except ValueError:
        san = x509.DNSName(address)
    public = csr.public_key()
    return (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca.certificate.subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=_LEAF_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.key.public_key()),
            critical=False,
        )
        .sign(ca.key, hashes.SHA256())
    )


def issued_by(certificate: x509.Certificate, ca: LabCa) -> bool:
    """True when ``certificate`` is directly issued and signed by ``ca``."""
    try:
        certificate.verify_directly_issued_by(ca.certificate)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True
