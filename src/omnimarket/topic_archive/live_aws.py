# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AWS bindings of the archive boundary: an S3 sink and a KMS envelope cipher.

* ``KmsEnvelopeArchiveCipher`` asks KMS for a fresh AES-256 data key per
  object (GenerateDataKey on the archive's customer managed key), encrypts the
  object locally with AES-256-GCM, and stores the KMS-wrapped data key in the
  object's own header, next to the ciphertext, so an object and the key that
  opens it can never be separated. The plaintext data key exists only in this
  process's memory. Decrypting calls KMS Decrypt on the wrapped key, so opening
  an archive needs the same KMS permission as creating one, and no key material
  is stored anywhere else.
* ``S3ArchiveSink`` writes each object under a key prefix with SSE-KMS naming
  the same key, then reads the object's head back and fails the write unless S3
  reports that encryption and that key. It leaves the host, so the archiver
  refuses to hand it anything a cipher has not encrypted.

Credentials come from the standard AWS credential chain of the process (an SSO
profile on the operator's Mac); nothing here reads or names a secret.

Envelope layout, all integers big-endian::

    b"ONEXKMS1" | u16 wrapped_key_len | wrapped_key | 12-byte nonce | AES-GCM ciphertext+tag

The first three fields are authenticated as associated data, so a swapped or
edited header fails decryption.
"""

from __future__ import annotations

import os
import struct
from pathlib import PurePosixPath
from typing import Any

from omnimarket.topic_archive.models import EnumArchiveEncryption

MAGIC = b"ONEXKMS1"
_NONCE_BYTES = 12
_LEN = struct.Struct(">H")
#: Bound into every data key; KMS refuses to unwrap a key under another context.
ENCRYPTION_CONTEXT = {"onex:purpose": "topic-archive"}


class KmsEnvelopeArchiveCipher:
    """Per-object data keys from KMS, AES-256-GCM locally, wrapped key in the header."""

    encryption = EnumArchiveEncryption.KMS_ENVELOPE_AES256GCM

    def __init__(self, *, client: Any, key_arn: str) -> None:
        self._kms = client
        self._key_arn = key_arn

    @property
    def recipient(self) -> str | None:
        return self._key_arn

    def encrypt(self, data: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        resp = self._kms.generate_data_key(
            KeyId=self._key_arn,
            KeySpec="AES_256",
            EncryptionContext=ENCRYPTION_CONTEXT,
        )
        wrapped: bytes = resp["CiphertextBlob"]
        header = MAGIC + _LEN.pack(len(wrapped)) + wrapped
        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(resp["Plaintext"]).encrypt(nonce, data, header)
        return header + nonce + sealed

    def decrypt(self, data: bytes) -> bytes:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not data.startswith(MAGIC) or len(data) < len(MAGIC) + _LEN.size:
            raise ValueError("not a KMS envelope: missing header")
        (n,) = _LEN.unpack_from(data, len(MAGIC))
        start = len(MAGIC) + _LEN.size
        header = data[: start + n]
        nonce = data[start + n : start + n + _NONCE_BYTES]
        sealed = data[start + n + _NONCE_BYTES :]
        if len(nonce) != _NONCE_BYTES or not sealed:
            raise ValueError("not a KMS envelope: truncated")
        key = self._kms.decrypt(
            CiphertextBlob=data[start : start + n],
            EncryptionContext=ENCRYPTION_CONTEXT,
            KeyId=self._key_arn,
        )["Plaintext"]
        try:
            out: bytes = AESGCM(key).decrypt(nonce, sealed, header)
        except InvalidTag as exc:
            raise ValueError("KMS envelope failed authentication") from exc
        return out


class S3ArchiveSink:
    """Objects under ``s3://bucket/prefix``, each written with SSE-KMS and head-checked."""

    requires_encryption = True

    def __init__(
        self, *, client: Any, bucket: str, prefix: str, kms_key_arn: str
    ) -> None:
        self._s3 = client
        self._bucket = bucket
        self._prefix = prefix if prefix.endswith("/") or not prefix else prefix + "/"
        self._key_arn = kms_key_arn
        self.location = f"s3://{bucket}/{self._prefix}"

    def _key(self, name: str) -> str:
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError(
                f"archive object name must be relative and inside the sink: {name!r}"
            )
        return self._prefix + rel.as_posix()

    def put(self, name: str, data: bytes) -> None:
        key = self._key(name)
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=self._key_arn,
        )
        head = self._s3.head_object(Bucket=self._bucket, Key=key)
        if (
            head.get("ServerSideEncryption") != "aws:kms"
            or head.get("SSEKMSKeyId") != self._key_arn
            or head.get("ContentLength") != len(data)
        ):
            raise RuntimeError(
                f"s3://{self._bucket}/{key} did not read back as SSE-KMS under "
                f"{self._key_arn} with {len(data)} bytes"
            )

    def get(self, name: str) -> bytes:
        body: bytes = self._s3.get_object(Bucket=self._bucket, Key=self._key(name))[
            "Body"
        ].read()
        return body

    def exists(self, name: str) -> bool:
        key = self._key(name)
        return key in self._keys(key)

    def _keys(self, key_prefix: str) -> list[str]:
        pages = self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self._bucket, Prefix=key_prefix
        )
        return [o["Key"] for page in pages for o in page.get("Contents", [])]

    def list_names(self, prefix: str) -> list[str]:
        full = self._prefix + prefix
        return sorted(k[len(self._prefix) :] for k in self._keys(full))


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """``s3://bucket/some/prefix/`` -> (bucket, "some/prefix/")."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// uri: {uri!r}")
    bucket, _, prefix = uri[len("s3://") :].partition("/")
    if not bucket:
        raise ValueError(f"no bucket in {uri!r}")
    return bucket, prefix


def aws_archive_boundary(
    *, s3_uri: str, kms_key: str, profile: str | None = None
) -> tuple[S3ArchiveSink, KmsEnvelopeArchiveCipher]:
    """The S3 sink and KMS cipher from the process's AWS credential chain.

    ``kms_key`` may be an alias; it is resolved to the key ARN once, because the
    bucket policy compares the SSE-KMS key id as a string and S3 reports the ARN.
    """
    import boto3

    session = boto3.session.Session(profile_name=profile)
    kms = session.client("kms")
    key_arn: str = kms.describe_key(KeyId=kms_key)["KeyMetadata"]["Arn"]
    bucket, prefix = parse_s3_uri(s3_uri)
    sink = S3ArchiveSink(
        client=session.client("s3"),
        bucket=bucket,
        prefix=prefix,
        kms_key_arn=key_arn,
    )
    return sink, KmsEnvelopeArchiveCipher(client=kms, key_arn=key_arn)
