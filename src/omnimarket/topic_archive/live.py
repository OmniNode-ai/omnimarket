# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live implementations of the archive boundary.

* ``LocalDirArchiveSink`` stages archives in a local directory, owner-only.
* ``AgeArchiveCipher`` encrypts to an age X25519 recipient. The archiver needs
  only the public recipient; decryption needs the identity, which only the
  replay side is given.

The broker side lives with the node that owns it, where the contract declares
the Kafka transport: the group-less topic reader in
``node_topic_archive_effect.handlers._kafka_topic_reader`` and the
replay producer in
``node_topic_archive_replay_effect.handlers._kafka_replay_writer``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath

from omnimarket.topic_archive.models import EnumArchiveEncryption
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
)


class NoArchiveCipher:
    """Identity transform. Refused by any sink that requires encryption."""

    encryption = EnumArchiveEncryption.NONE
    recipient: str | None = None

    def encrypt(self, data: bytes) -> bytes:
        return data

    def decrypt(self, data: bytes) -> bytes:
        return data


class AgeArchiveCipher:
    """age (X25519) encryption; ``identity`` is only needed to decrypt."""

    encryption = EnumArchiveEncryption.AGE_X25519

    def __init__(self, *, recipient: str, identity: str | None = None) -> None:
        import pyrage

        self.recipient: str | None = recipient
        self._recipient = pyrage.x25519.Recipient.from_str(recipient)
        self._identity = pyrage.x25519.Identity.from_str(identity) if identity else None

    def encrypt(self, data: bytes) -> bytes:
        import pyrage

        out: bytes = pyrage.encrypt(data, [self._recipient])
        return out

    def decrypt(self, data: bytes) -> bytes:
        import pyrage

        if self._identity is None:
            raise RuntimeError(
                "decryption needs the age identity; this cipher holds only the recipient"
            )
        out: bytes = pyrage.decrypt(data, [self._identity])
        return out


class LocalDirArchiveSink:
    """A local staging directory: dirs 0700, files 0600, atomic writes."""

    requires_encryption = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.location = f"file://{root}"

    def _path(self, name: str) -> Path:
        rel = PurePosixPath(name)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ValueError(
                f"archive object name must be relative and inside the sink: {name!r}"
            )
        return self.root.joinpath(*rel.parts)

    def put(self, name: str, data: bytes) -> None:
        path = self._path(name)
        self._owner_only_dirs(path.parent)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _owner_only_dirs(self, directory: Path) -> None:
        """Create the sink root and every directory under it with mode 0700.

        Path.mkdir(parents=True, mode=...) applies the mode to the last
        directory only, so the root and intermediate levels would otherwise
        take the process umask.
        """
        chain = [directory, *directory.parents]
        stop = chain.index(self.root) + 1 if self.root in chain else len(chain)
        for d in reversed(chain[:stop]):
            if not d.exists():
                d.mkdir(mode=0o700)
                os.chmod(d, 0o700)

    def get(self, name: str) -> bytes:
        return self._path(name).read_bytes()

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def list_names(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        names = (
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.startswith(".tmp-")
        )
        return sorted(n for n in names if n.startswith(prefix))


#: Addressing for runtime dispatch: the staging directory and the age
#: recipient/identity are read when first used.
STAGING_DIR_ENV = "ONEX_TOPIC_ARCHIVE_STAGING_DIR"
AGE_RECIPIENT_ENV = "ONEX_TOPIC_ARCHIVE_AGE_RECIPIENT"
AGE_IDENTITY_ENV = "ONEX_TOPIC_ARCHIVE_AGE_IDENTITY"


class _LazySink:
    """A LocalDirArchiveSink whose directory is resolved on first use."""

    requires_encryption = False

    def _sink(self) -> LocalDirArchiveSink:
        return LocalDirArchiveSink(Path(os.environ[STAGING_DIR_ENV]))

    @property
    def location(self) -> str:
        return self._sink().location

    def put(self, name: str, data: bytes) -> None:
        self._sink().put(name, data)

    def get(self, name: str) -> bytes:
        return self._sink().get(name)

    def exists(self, name: str) -> bool:
        return self._sink().exists(name)

    def list_names(self, prefix: str) -> list[str]:
        return self._sink().list_names(prefix)


def _cipher_from_env(*, with_identity: bool) -> NoArchiveCipher | AgeArchiveCipher:
    recipient = os.environ.get(AGE_RECIPIENT_ENV)
    if not recipient:
        return NoArchiveCipher()
    identity = os.environ[AGE_IDENTITY_ENV] if with_identity else None
    return AgeArchiveCipher(recipient=recipient, identity=identity)


class _LazyCipher:
    """Resolves the cipher from the environment on first use."""

    def __init__(self, *, with_identity: bool) -> None:
        self._with_identity = with_identity

    def _c(self) -> NoArchiveCipher | AgeArchiveCipher:
        return _cipher_from_env(with_identity=self._with_identity)

    @property
    def encryption(self) -> EnumArchiveEncryption:
        return self._c().encryption

    @property
    def recipient(self) -> str | None:
        return self._c().recipient

    def encrypt(self, data: bytes) -> bytes:
        return self._c().encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        return self._c().decrypt(data)


def live_archive_boundary() -> tuple[ProtocolArchiveSink, ProtocolArchiveCipher]:
    return _LazySink(), _LazyCipher(with_identity=False)


def live_replay_boundary() -> tuple[ProtocolArchiveSink, ProtocolArchiveCipher]:
    return _LazySink(), _LazyCipher(with_identity=True)
