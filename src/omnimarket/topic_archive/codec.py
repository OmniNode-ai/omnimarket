# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure encoding for topic archives. No I/O.

Format: JSON Lines, one broker record per line, gzip-compressed with a fixed
mtime so equal input always yields equal bytes and a checksum is reproducible.
JSONL rather than Parquet because the hook topics carry heterogeneous JSON
values per topic and replay needs the exact bytes back, not a columnar schema;
JSONL streams, needs nothing beyond the standard library to read, and a
human can inspect a day with ``zcat | head``.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
from collections.abc import Iterable

from omnimarket.topic_archive.models import EnumArchiveEncryption, ModelArchivedRecord


def utc_day(timestamp_ms: int) -> dt.date:
    """The UTC calendar day a broker timestamp falls on."""
    return dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.UTC).date()


def record_line(record: ModelArchivedRecord) -> bytes:
    """One compact, key-sorted JSON line terminated by a newline."""
    return (
        json.dumps(
            record.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        + "\n"
    ).encode("utf-8")


def encode_lines(records: Iterable[ModelArchivedRecord]) -> bytes:
    return b"".join(record_line(r) for r in records)


def decode_lines(data: bytes) -> list[ModelArchivedRecord]:
    return [
        ModelArchivedRecord.model_validate_json(line)
        for line in data.splitlines()
        if line
    ]


def gzip_deterministic(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9
    ) as gz:
        gz.write(data)
    return buf.getvalue()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def object_name(
    topic: str,
    partition: int,
    day: dt.date,
    first_offset: int,
    last_offset: int,
    encryption: EnumArchiveEncryption,
) -> str:
    suffix = ".age" if encryption is EnumArchiveEncryption.AGE_X25519 else ""
    return (
        f"{topic}/partition={partition}/day={day.isoformat()}/"
        f"offsets-{first_offset}-{last_offset}.jsonl.gz{suffix}"
    )


def manifest_name(object_name_: str) -> str:
    return object_name_.split(".jsonl.gz")[0] + ".manifest.json"
