"""Pure, bounded OCI repository and digest-reference grammar validation."""

from __future__ import annotations

import ipaddress

_MAX_REPOSITORY_BYTES = 240
_MAX_REFERENCE_BYTES = 312
_SHA256_HEX_LENGTH = 64
_REFERENCE_SEPARATOR = "@sha256:"


def _invalid() -> ValueError:
    return ValueError("OCI repository is invalid")


def _is_lower_ascii(value: str) -> bool:
    return value.isascii() and value == value.lower()


def _validate_port(value: str) -> None:
    if not value or not value.isascii() or not value.isdecimal():
        raise _invalid()
    if len(value) > 1 and value.startswith("0"):
        raise _invalid()
    number = int(value)
    if not 1 <= number <= 65_535:
        raise _invalid()


def _is_dns_label(value: str) -> bool:
    return (
        1 <= len(value) <= 63
        and value[0].isalnum()
        and value[-1].isalnum()
        and all(char.isalnum() or char == "-" for char in value)
    )


def _validate_dns_or_ipv4(value: str) -> None:
    if not value or not _is_lower_ascii(value):
        raise _invalid()
    labels = value.split(".")
    if any(not _is_dns_label(label) for label in labels):
        raise _invalid()
    # A lone numeric label remains a V1 DNS label. Any dotted all-numeric
    # authority is instead an IPv4 claim, never a DNS fallback.
    if "." in value and all(label.isdecimal() for label in labels):
        try:
            parsed = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            raise _invalid() from None
        if str(parsed) != value:
            raise _invalid()


def _validate_authority(value: str) -> None:
    if not value or not value.isascii():
        raise _invalid()
    if value.startswith("["):
        close = value.find("]")
        if close <= 1:
            raise _invalid()
        host, suffix = value[1:close], value[close + 1 :]
        if not suffix:
            pass
        elif suffix.startswith(":"):
            _validate_port(suffix[1:])
        else:
            raise _invalid()
        if "%" in host or not _is_lower_ascii(host):
            raise _invalid()
        try:
            parsed = ipaddress.IPv6Address(host)
        except ipaddress.AddressValueError:
            raise _invalid() from None
        if str(parsed) != host:
            raise _invalid()
        return

    if "[" in value or "]" in value or value.count(":") > 1:
        raise _invalid()
    host, separator, port = value.partition(":")
    if separator:
        _validate_port(port)
    _validate_dns_or_ipv4(host)


def _validate_component(value: str) -> None:
    if not value or not _is_lower_ascii(value):
        raise _invalid()
    expecting_alnum = True
    for char in value:
        if expecting_alnum:
            if not char.isalnum():
                raise _invalid()
        elif not (char.isalnum() or char in "._-"):
            raise _invalid()
        expecting_alnum = not char.isalnum()
    if expecting_alnum:
        raise _invalid()


def validate_oci_repository_v1(value: str) -> str:
    """Return only the canonical OCI repository spelling described by V1."""
    if type(value) is not str or not _is_lower_ascii(value):
        raise _invalid()
    try:
        byte_count = len(value.encode("ascii"))
    except UnicodeEncodeError:
        raise _invalid() from None
    if not 1 <= byte_count <= _MAX_REPOSITORY_BYTES:
        raise _invalid()
    authority, separator, remainder = value.partition("/")
    if not separator or not remainder:
        raise _invalid()
    _validate_authority(authority)
    for component in remainder.split("/"):
        _validate_component(component)
    return value


def oci_repository_reference_v1(repository: str, digest_sha256: str) -> str:
    """Construct a canonical digest-only OCI reference from validated inputs."""
    repository = validate_oci_repository_v1(repository)
    if (
        type(digest_sha256) is not str
        or len(digest_sha256) != _SHA256_HEX_LENGTH
        or not digest_sha256.isascii()
        or any(char not in "0123456789abcdef" for char in digest_sha256)
    ):
        raise ValueError("OCI reference is invalid")
    reference = f"{repository}{_REFERENCE_SEPARATOR}{digest_sha256}"
    if len(reference.encode("ascii")) > _MAX_REFERENCE_BYTES:
        raise ValueError("OCI reference is invalid")
    return reference


def validate_oci_repository_reference_v1(value: str) -> str:
    """Return only a canonical V1 repository plus lower-hex SHA-256 reference."""
    if (
        type(value) is not str
        or not value.isascii()
        or len(value.encode("ascii")) > _MAX_REFERENCE_BYTES
    ):
        raise ValueError("OCI reference is invalid")
    repository, separator, digest_sha256 = value.partition(_REFERENCE_SEPARATOR)
    if not separator or value != oci_repository_reference_v1(repository, digest_sha256):
        raise ValueError("OCI reference is invalid")
    return value
