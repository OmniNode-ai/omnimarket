# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real backend: the lane's served manifest over HTTP, the lane's broker over aiokafka, a declared broker's consumer groups over the confluent_kafka admin client, the broker's external listener over TCP, a state file.

The lane's broker address and login come from the lane declaration the dev-lane clients resolve, never
from an ambient variable; ``AdminLag`` reads the address its overlay declares. Nothing here prints or
returns a credential or a broker address: a failure is named by its exception type only.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import re
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_effects_held import (
    ModelEffectsGroupReading,
    ModelEscalationBrief,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelEscalationSettings
from omnimarket.nodes.node_lab_alarm.protocols import (
    EffectsGroupReadError,
    GroupNeverCommittedError,
    GroupUnreadableError,
    HeartbeatUnreadableError,
    LaneDispatchError,
    LedgerUnreadableError,
    ListenerRefusedError,
    ListenerUnresolvableError,
    ManifestUnreachableError,
    ManifestUnreadableError,
    RunRecordUnwritableError,
)


class FileLedgerNewest:
    """The newest canonical row in the tail of the ledger file: ``<UTC stamp> | <TYPE> |`` with TYPE in ``row_types``.

    Only the last ``tail_bytes`` are read, so a row earlier than that is never seen. A file that cannot be read, or
    whose tail holds no such row, is unread, never current.
    """

    def __init__(self, path: Path, tail_bytes: int, row_types: Sequence[str]) -> None:
        self._path = path
        self._tail_bytes = tail_bytes
        self._pattern = re.compile(
            r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| "
            rf"(?P<type>{'|'.join(re.escape(row_type) for row_type in row_types)}) \|",
            re.MULTILINE,
        )

    def newest(self) -> tuple[datetime, str]:
        try:
            with self._path.open("rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                start = max(0, size - self._tail_bytes)
                handle.seek(start)
                raw = handle.read()
        except OSError as exc:
            raise LedgerUnreadableError(
                f"ledger could not be read ({type(exc).__name__})"
            ) from exc
        text = raw.decode("utf-8", errors="replace")
        if start > 0:
            # The first line of a mid-file read is a fragment, not a row.
            text = text.partition("\n")[2]
        newest: tuple[datetime, str] | None = None
        for match in self._pattern.finditer(text):
            stamp = datetime.strptime(match.group("ts"), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
            if newest is None or stamp > newest[0]:
                newest = (stamp, match.group("type"))
        if newest is None:
            raise LedgerUnreadableError(
                f"ledger holds no canonical row in its last {self._tail_bytes} bytes"
            )
        return newest


class HttpHeartbeatSource:
    """The deploy agent's heartbeat document: one GET of the full URL, read as a JSON object.

    Anything but an HTTP 200 carrying a JSON object is unread, never an empty document.
    """

    def __init__(self, url: str, timeout_seconds: float) -> None:
        self._url = url
        self._timeout = timeout_seconds

    def read(self) -> Mapping[str, Any]:
        if not self._url.startswith("http://"):
            raise HeartbeatUnreadableError(f"{self._url!r} is not an http URL")
        try:
            with (
                urllib.request.urlopen(  # no-contract-check: the deploy agent's heartbeat read; port boundary, url injected by the overlay-built reader
                    self._url, timeout=self._timeout
                ) as response
            ):
                if response.status != 200:
                    raise HeartbeatUnreadableError(
                        f"the agent answered HTTP {response.status}"
                    )
                payload = json.loads(response.read())
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise HeartbeatUnreadableError(
                f"no answer from the agent ({type(exc).__name__})"
            ) from exc
        except ValueError as exc:
            raise HeartbeatUnreadableError("the agent's answer is not JSON") from exc
        if not isinstance(payload, dict):
            raise HeartbeatUnreadableError("the agent's answer is not a JSON object")
        return payload


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
                with (
                    urllib.request.urlopen(  # no-contract-check: the lane's served-manifest read; port boundary, url injected by the overlay-built reader
                        self._url, timeout=self._timeout
                    ) as response
                ):
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


def lane_client_kwargs(lane: str, timeout_ms: int) -> dict[str, Any]:
    """The admin client settings of the lane, from the lane declaration the dev-lane clients resolve."""
    from omnibase_infra.cli.delegate_lane_credentials import resolve_lane_binding

    selection, transport = resolve_lane_binding(lane)
    kwargs: dict[str, Any] = {
        "bootstrap_servers": selection.bootstrap_servers,
        "security_protocol": selection.security_protocol,
        "sasl_mechanism": selection.sasl_mechanism,
        "request_timeout_ms": timeout_ms,
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


class KafkaLag:
    """Committed offsets against partition end offsets on the lane's broker."""

    def __init__(self, lane: str, timeout_seconds: float) -> None:
        self._lane = lane
        self._timeout_ms = int(timeout_seconds * 1000)

    def _client_kwargs(self) -> dict[str, Any]:
        return lane_client_kwargs(self._lane, self._timeout_ms)

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
        consumer = AIOKafkaConsumer(  # no-contract-check: end-offsets read for the lane's own broker; port boundary behind ProtocolGroupLagReader
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


def _committed_request(group: str) -> Any:
    """The read-committed request for one group; built lazily so the import stays lazy."""
    from confluent_kafka import ConsumerGroupTopicPartitions

    return ConsumerGroupTopicPartitions(group)


def _end_offset_key(topic: str, partition: int) -> tuple[Any, Any]:
    """One list-offsets key with its latest-offset spec; built lazily so the import stays lazy."""
    from confluent_kafka import TopicPartition

    # The class confluent_kafka.admin re-exports, from the module that types it.
    from confluent_kafka.admin._listoffsets import OffsetSpec

    latest: Callable[[], Any] = OffsetSpec.latest
    return TopicPartition(topic, partition), latest()


def _end_offsets(
    admin: Any, keys: list[tuple[str, int]], timeout_seconds: float
) -> dict[tuple[str, int], int]:
    if not keys:
        return {}
    futures = admin.list_offsets(
        dict(_end_offset_key(topic, partition) for topic, partition in keys),
        request_timeout=timeout_seconds,
    )
    return {
        (tp.topic, tp.partition): int(f.result(timeout=timeout_seconds).offset)
        for tp, f in futures.items()
    }


class AdminLag:
    """Committed offsets against partition end offsets on one declared broker, the admin client only.

    No consumer is built and no group is joined, so reading a group moves nothing, and the read
    carries no credential: the address is the overlay's ``bootstrap_servers`` alone. A broker failure
    is named by its exception type only, never its text, which carries the address.
    """

    def __init__(
        self,
        bootstrap_servers: str,
        timeout_seconds: float,
        admin_factory: Callable[[dict[str, str]], Any] | None = None,
    ) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._timeout = timeout_seconds
        self._admin_factory = admin_factory

    def total_lag(self, group: str) -> int:
        try:
            return self._read(group)
        except (GroupNeverCommittedError, GroupUnreadableError):
            raise
        except Exception as exc:  # classify by type, never echo str(exc)
            raise GroupUnreadableError(
                f"group {group} unreadable ({type(exc).__name__})"
            ) from None

    def _read(self, group: str) -> int:
        from confluent_kafka.admin import AdminClient

        admin = (self._admin_factory or AdminClient)(
            {"bootstrap.servers": self._bootstrap_servers}
        )
        future = admin.list_consumer_group_offsets(
            [_committed_request(group)], request_timeout=self._timeout
        )[group]
        committed = future.result(timeout=self._timeout).topic_partitions
        held = [tp for tp in committed if int(tp.offset) >= 0]
        if not held:
            raise GroupNeverCommittedError(
                f"group {group} holds no committed offset on any partition"
            )
        ends = _end_offsets(
            admin, [(tp.topic, tp.partition) for tp in held], self._timeout
        )
        total = 0
        for tp in held:
            end = ends[(tp.topic, tp.partition)]
            total += max(0, end - int(tp.offset))
        return total


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


class FileGrowthStreaks:
    """Each group's count of consecutive growth samples, one JSON object written whole through a temporary file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, int]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            for k, v in raw.items()
        ):
            return {}
        return dict(raw)

    def save(self, streaks: Mapping[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(streaks), sort_keys=True), encoding="utf-8")
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


class KafkaEffectsGroups:
    """Every group on the lane's broker matching the prefix and suffix, with its state and live member count."""

    def __init__(
        self, lane: str, timeout_seconds: float, prefix: str, suffix: str
    ) -> None:
        self._lane = lane
        self._timeout_ms = int(timeout_seconds * 1000)
        self._prefix = prefix
        self._suffix = suffix

    async def _read(self) -> tuple[ModelEffectsGroupReading, ...]:
        from aiokafka.admin import AIOKafkaAdminClient

        kwargs = lane_client_kwargs(self._lane, self._timeout_ms)
        security_protocol = kwargs.pop("security_protocol")
        admin = AIOKafkaAdminClient(
            client_id="effects-held-alarm-admin",
            security_protocol=security_protocol,
            **kwargs,
        )
        await admin.start()
        try:
            listed = await admin.list_consumer_groups()
            names = (g[0] if isinstance(g, tuple) else g for g in listed)
            wanted = [
                n
                for n in names
                if n.startswith(self._prefix) and n.endswith(self._suffix)
            ]
            readings: list[ModelEffectsGroupReading] = []
            for name in wanted:
                # One group per describe call: batching group ids corrupts the decode on this broker.
                for response in await admin.describe_consumer_groups([name]):
                    for entry in response.groups:
                        # (error_code, group, state, protocol_type, protocol, members), by position.
                        readings.append(
                            ModelEffectsGroupReading(
                                group=entry[1],
                                state=entry[2],
                                members=len(entry[5]),
                            )
                        )
            return tuple(readings)
        finally:
            await admin.close()

    def read(self) -> tuple[ModelEffectsGroupReading, ...]:
        try:
            return asyncio.run(self._read())
        except Exception as exc:  # classify by type, never echo str(exc)
            raise EffectsGroupReadError(f"unreadable ({type(exc).__name__})") from None


class FileHeldState:
    """The effects-held episode state, one JSON object of strings, written whole through a temporary file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, str]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            return {}
        return dict(raw)

    def save(self, state: Mapping[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(state), sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)


def _brief_text(brief: ModelEscalationBrief) -> str:
    return (
        "Lab alarm escalation\n\n"
        f"Condition: {brief.condition}\nSubject: {brief.subject}\n"
        f"Raised at: {brief.raised_at}\nDetail: {brief.detail}\n"
        f"Evidence: {brief.evidence}\n\n"
        "The alarm raised and nothing owns the condition. Find why the consumer is absent, "
        "read the dev deploy agent's job list and any live code-ref pin before touching "
        "anything, and restore it through the declared route (an agent job or the idle "
        "restore), never a raw container command, a broker restart or an offset reset. "
        "Never widen the alarm, mute it or change its bound. Cite the condition and "
        "raised_at in your CLAIM. A cause that lives only on a host goes in your final "
        "message with the observed text as evidence.\n"
    )


class CommandLaneDispatcher:
    """Starts one detached lane through the overlay's dispatch command, the shape the host drift escalation uses."""

    def __init__(self, settings: ModelEscalationSettings) -> None:
        self._settings = settings

    def dispatch(self, brief: ModelEscalationBrief) -> None:
        stamp = re.sub(r"[^0-9]", "", brief.raised_at)[:12]
        name = re.sub(r"[^a-z0-9-]", "-", brief.condition.lower().replace("_", "-"))[
            :30
        ]
        folder = Path(self._settings.brief_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            brief_path = folder / f"{name}-{stamp}-brief.md"
            brief_path.write_text(_brief_text(brief), encoding="utf-8")
            with (folder / f"{name}-{stamp}-dispatch.log").open("ab") as output:
                runner = subprocess.Popen(
                    [
                        *self._settings.command,
                        "--brief",
                        str(brief_path),
                        "--lane",
                        f"lab-alarm-{name}-{stamp}",
                        "--model",
                        self._settings.model,
                        "--ticket",
                        self._settings.ticket,
                    ],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                )
            # A runner that refuses to start exits at once with its reason in the log; one still
            # running past the window is a lane started.
            try:
                code = runner.wait(timeout=self._settings.settle_seconds)
            except subprocess.TimeoutExpired:
                return
        except OSError as exc:
            raise LaneDispatchError(f"dispatch failed ({type(exc).__name__})") from None
        if code != 0:
            raise LaneDispatchError(f"dispatch exited {code}")


_API_VERSIONS_KEY = 18
_API_VERSIONS_VERSION = 0
_CORRELATION_ID = 23933
_CLIENT_ID = "lab-alarm"


class TcpApiVersionsProbe:
    """One unauthenticated Kafka ApiVersions request over TCP; the header back means the listener answers.

    ApiVersions is answered before authentication, so nothing here sends or reads a credential: the
    request carries only the api key, its version, the correlation id and the client id, and only
    the response's 8-byte header (length and correlation id) is read. A name that will not resolve
    is the alarm host's fault; every other failure is the listener's refusal.
    """

    def probe(self, host: str, port: int, timeout: float) -> None:
        client_id = _CLIENT_ID.encode("utf-8")
        payload = (
            struct.pack(
                ">hhi", _API_VERSIONS_KEY, _API_VERSIONS_VERSION, _CORRELATION_ID
            )
            + struct.pack(">h", len(client_id))
            + client_id
        )
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                sock.sendall(struct.pack(">i", len(payload)) + payload)
                header = b""
                while len(header) < 8:
                    chunk = sock.recv(8 - len(header))
                    if not chunk:
                        raise ListenerRefusedError(
                            f"{host}:{port} closed before the response header"
                        )
                    header += chunk
        except socket.gaierror as exc:
            raise ListenerUnresolvableError(f"{type(exc).__name__}: {exc}") from None
        except OSError as exc:
            raise ListenerRefusedError(f"{type(exc).__name__}: {exc}") from None
        if struct.unpack(">i", header[4:])[0] != _CORRELATION_ID:
            raise ListenerRefusedError(
                f"{host}:{port} answered with a foreign correlation id"
            )


__all__ = [
    "AdminLag",
    "CommandLaneDispatcher",
    "FileGrowthStreaks",
    "FileHeldState",
    "FileLedgerNewest",
    "FileManifestCache",
    "FileRunRecords",
    "FileSamples",
    "HttpHeartbeatSource",
    "HttpManifest",
    "KafkaEffectsGroups",
    "KafkaLag",
    "TcpApiVersionsProbe",
    "lane_client_kwargs",
]
