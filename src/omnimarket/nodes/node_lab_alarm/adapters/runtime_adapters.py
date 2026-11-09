# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real backend: the lane's served manifest over HTTP, the lane's broker over aiokafka, a state file.

The broker address and login come from the lane declaration the dev-lane clients resolve, never from an
ambient variable. Nothing here prints or returns a credential or a broker address: a failure is named by
its exception type only.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_lab_alarm.protocols import (
    GroupNeverCommittedError,
    GroupUnreadableError,
    ManifestUnreachableError,
    ManifestUnreadableError,
    RunRecordUnwritableError,
)


class HttpManifest:
    """The served manifest. A transport failure is retried ``attempts`` times, then it is unreachable.

    The runtime serves the manifest only once its kernel is up, so the endpoint refuses or resets for
    minutes while dev redeploys; a bounded retry rides out a blip, the handler's fallback the rest.
    A body that arrived and cannot be read is not retried: that is a different fault.
    """

    def __init__(
        self,
        url: str,
        timeout_seconds: float,
        *,
        attempts: int = 1,
        retry_delay_seconds: float = 0.0,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self._url = url
        self._timeout = timeout_seconds
        self._attempts = attempts
        self._delay = retry_delay_seconds
        self._sleep = sleep

    def read(self) -> Mapping[str, Any]:
        failure = "OSError"
        for attempt in range(1, self._attempts + 1):
            try:
                with urllib.request.urlopen(
                    self._url, timeout=self._timeout
                ) as response:
                    payload = json.loads(response.read())
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                http.client.HTTPException,
            ) as exc:
                failure = type(exc).__name__
                if attempt < self._attempts:
                    self._sleep(self._delay)
                continue
            except ValueError:
                raise ManifestUnreadableError("manifest body is not JSON") from None
            if not isinstance(payload, dict):
                raise ManifestUnreadableError("manifest body is not an object")
            return payload
        raise ManifestUnreachableError(
            f"manifest fetch failed ({failure}) after {self._attempts} "
            f"attempt{'s' if self._attempts != 1 else ''}"
        )


class FileManifestCache:
    """The last good manifest on disk: the declared nodes' contracts and the time they were fetched."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> tuple[Mapping[str, Any], datetime] | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            fetched_at = datetime.fromisoformat(raw["fetched_at"])
            manifest = raw["manifest"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        if not isinstance(manifest, dict):
            return None
        return manifest, fetched_at

    def save(self, manifest: Mapping[str, Any], fetched_at: datetime) -> None:
        # A cache that cannot be written only means no fallback next time; it never fails the run.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {"fetched_at": fetched_at.isoformat(), "manifest": dict(manifest)}
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            return


class KafkaLag:
    """Committed offsets against partition end offsets on the lane's broker."""

    def __init__(self, lane: str, timeout_seconds: float) -> None:
        self._lane = lane
        self._timeout_ms = int(timeout_seconds * 1000)

    def _client_kwargs(self) -> dict[str, Any]:
        from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding

        selection, transport = resolve_lane_binding(self._lane)
        kwargs: dict[str, Any] = {
            "bootstrap_servers": selection.bootstrap_servers,
            "security_protocol": selection.security_protocol,
            "sasl_mechanism": selection.sasl_mechanism,
            "request_timeout_ms": self._timeout_ms,
        }
        if transport is not None:
            overrides = transport.as_client_config_overrides()
            kwargs.update(
                {
                    key: overrides[key]
                    for key in (
                        "security_protocol",
                        "sasl_mechanism",
                        "sasl_plain_username",
                        "sasl_plain_password",
                    )
                    if key in overrides
                }
            )
        return kwargs

    async def _read(self, group: str) -> int:
        from aiokafka import AIOKafkaConsumer
        from aiokafka.admin import AIOKafkaAdminClient

        kwargs = self._client_kwargs()
        security_protocol = kwargs.pop("security_protocol")
        admin = AIOKafkaAdminClient(
            client_id="broker-lag-alarm-admin",
            security_protocol=security_protocol,
            **kwargs,
        )
        await admin.start()
        try:
            offsets = await admin.list_consumer_group_offsets(group)
        finally:
            await admin.close()
        if not offsets:
            raise GroupNeverCommittedError(
                f"group {group} holds no committed offset on any partition"
            )
        consumer = AIOKafkaConsumer(
            client_id="broker-lag-alarm-consumer",
            security_protocol=security_protocol,
            **kwargs,
        )
        await consumer.start()
        try:
            ends = await consumer.end_offsets(list(offsets.keys()))
        finally:
            await consumer.stop()
        total = 0
        for partition, metadata in offsets.items():
            end = ends.get(partition)
            if end is None:
                raise GroupUnreadableError(
                    f"group {group} has no end offset for {partition.topic}[{partition.partition}]"
                )
            total += max(0, end - metadata.offset)
        return total

    def total_lag(self, group: str) -> int:
        try:
            return asyncio.run(self._read(group))
        except (GroupNeverCommittedError, GroupUnreadableError):
            raise
        except Exception as exc:  # classify by type, never echo str(exc)
            raise GroupUnreadableError(
                f"group {group} unreadable ({type(exc).__name__})"
            ) from None


class FileSamples:
    """The previous sample, one JSON object of group to lag, written whole through a temporary file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, int] | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(v, int) for k, v in raw.items()
        ):
            return None
        return dict(raw)

    def save(self, sample: Mapping[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sample), sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)


class FileRunRecords:
    """The run record the floor reads: one JSON object per finished run, one line each, appended."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, record: Mapping[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(record), sort_keys=True) + "\n")
        except OSError as exc:
            raise RunRecordUnwritableError(
                f"run record unwritable ({type(exc).__name__})"
            ) from None


__all__ = [
    "FileManifestCache",
    "FileRunRecords",
    "FileSamples",
    "HttpManifest",
    "KafkaLag",
]
