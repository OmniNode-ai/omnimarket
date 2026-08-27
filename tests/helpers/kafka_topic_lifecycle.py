# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explicit, isolated Kafka topic lifecycle for integration tests.

Kafka brokers do not create a topic when a consumer subscribes to it.  The
smoke tests therefore use this helper to create a unique topic before
subscription and delete only that topic after the test.  The bootstrap and
authentication settings are supplied by the already-started event bus; this
module intentionally has no environment-variable or default-broker lookup.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol, cast
from uuid import uuid4

from aiokafka.admin import NewTopic
from aiokafka.errors import KafkaError, UnknownTopicOrPartitionError

from tests.helpers.topics import KAFKA_SMOKE_TOPIC_PREFIX

_UNKNOWN_TOPIC_OR_PARTITION_ERROR = 3
_GROUP_PREFIX = "omnimarket-integration-smoke"
_PARTITION_COUNT = 1
_REPLICATION_FACTOR = 1
logger = logging.getLogger(__name__)


class KafkaAdminClient(Protocol):
    """Small typed seam for the admin operations used by this helper."""

    async def start(self) -> None:
        """Open the admin connection."""

    async def close(self) -> None:
        """Close the admin connection."""

    async def create_topics(self, topics: list[NewTopic]) -> object:
        """Create the supplied topics and return the broker response."""

    async def delete_topics(self, topics: list[str]) -> object:
        """Delete the supplied topics and return the broker response."""

    async def describe_topics(self, topics: list[str]) -> object:
        """Return metadata for the supplied topics."""


KafkaAdminFactory = Callable[..., KafkaAdminClient]
MetadataWaiter = Callable[..., Awaitable[bool]]


class KafkaSmokeTopic(Protocol):
    """Typed topic/group shape consumed by the integration smoke test."""

    @property
    def topic(self) -> str:
        """The topic allocated for the test."""

    @property
    def group_id(self) -> str:
        """The consumer group allocated for the test."""


class KafkaSmokeTopicIdentity:
    """Unique topic and consumer group allocated for one smoke-test run."""

    __slots__ = ("group_id", "topic")

    def __init__(self, *, suffix: str | None = None) -> None:
        run_suffix = suffix or uuid4().hex
        self.topic = f"{KAFKA_SMOKE_TOPIC_PREFIX}-{run_suffix}.v1"
        self.group_id = f"{_GROUP_PREFIX}-{run_suffix}"


def _field(value: object, name: str) -> object | None:
    """Read a field from either aiokafka's object or mapping response shape."""
    if isinstance(value, Mapping):
        return cast(object | None, value.get(name))
    return cast(object | None, getattr(value, name, None))


def _topic_description(description: object, topic_name: str) -> object | None:
    """Find one topic in aiokafka's list or mapping metadata response."""
    if isinstance(description, Mapping):
        if description.get("topic") == topic_name:
            return description
        return cast(object | None, description.get(topic_name))

    if isinstance(description, Sequence) and not isinstance(
        description, (str, bytes, bytearray)
    ):
        for item in description:
            if _field(item, "topic") == topic_name:
                return cast(object, item)
    return None


def _topic_ready(
    description: object,
    topic_name: str,
    expected_partitions: int,
) -> bool:
    """Return whether metadata confirms a topic and its partitions exist."""
    topic_info = _topic_description(description, topic_name)
    if topic_info is None:
        return False

    error_code = _field(topic_info, "error_code")
    partitions = _field(topic_info, "partitions")
    if error_code not in (None, 0):
        return False
    return isinstance(partitions, Sequence) and len(partitions) >= expected_partitions


def _topic_absent(description: object, topic_name: str) -> bool:
    """Return whether metadata confirms that a topic is absent."""
    topic_info = _topic_description(description, topic_name)
    if topic_info is None:
        return True
    return _field(topic_info, "error_code") == _UNKNOWN_TOPIC_OR_PARTITION_ERROR


async def wait_for_topic_metadata(
    admin_client: KafkaAdminClient,
    topic_name: str,
    *,
    timeout_seconds: float = 10.0,
    expected_partitions: int = _PARTITION_COUNT,
    poll_interval_seconds: float = 0.25,
) -> bool:
    """Poll until a newly-created topic has the expected broker metadata."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if expected_partitions <= 0:
        raise ValueError("expected_partitions must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            description = await admin_client.describe_topics([topic_name])
            if _topic_ready(description, topic_name, expected_partitions):
                return True
        except (KafkaError, OSError):
            # Topic metadata can lag the create response.  Connection and
            # broker metadata errors are retried until the bounded deadline.
            pass

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_interval_seconds, remaining))


async def wait_for_topic_absence(
    admin_client: KafkaAdminClient,
    topic_name: str,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.25,
) -> bool:
    """Poll until broker metadata confirms that a deleted topic is absent."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            description = await admin_client.describe_topics([topic_name])
            if _topic_absent(description, topic_name):
                return True
        except UnknownTopicOrPartitionError:
            return True
        except (KafkaError, OSError):
            pass

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_interval_seconds, remaining))


def _response_entries(response: object, operation: str) -> Sequence[object]:
    """Extract version-compatible topic error entries from an admin response."""
    attributes = (
        ("topic_errors", "topic_error_codes")
        if operation == "create"
        else ("topic_error_codes", "topic_errors")
    )
    for attribute in attributes:
        entries = getattr(response, attribute, None)
        if entries is not None:
            if isinstance(entries, Sequence) and not isinstance(
                entries, (str, bytes, bytearray)
            ):
                return entries
            raise TypeError(
                f"Kafka {operation} response field {attribute!r} must be a sequence"
            )
    raise TypeError(
        f"Kafka {operation} response has no supported topic-error field; "
        f"got {type(response).__name__}"
    )


def _entry_error(entry: object) -> tuple[str, int]:
    """Extract a topic name and integer error code from a Kafka response entry."""
    if isinstance(entry, Mapping):
        raw_topic = entry.get("topic")
        raw_code = entry.get("error_code")
    elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes, bytearray)):
        if len(entry) < 2:
            raise TypeError(f"Kafka topic-error entry is too short: {entry!r}")
        raw_topic = entry[0]
        raw_code = entry[1]
    else:
        raise TypeError(f"Kafka topic-error entry has unsupported shape: {entry!r}")

    if not isinstance(raw_topic, str):
        raise TypeError(f"Kafka topic-error name must be a string: {raw_topic!r}")
    if isinstance(raw_code, bool) or not isinstance(raw_code, int):
        raise TypeError(f"Kafka topic-error code must be an integer: {raw_code!r}")
    return raw_topic, raw_code


def _assert_operation_succeeded(
    response: object,
    *,
    operation: str,
    expected_topic: str,
) -> None:
    """Fail closed on any broker-reported create/delete error."""
    entries = _response_entries(response, operation)
    for entry in entries:
        topic_name, error_code = _entry_error(entry)
        if error_code != 0:
            raise RuntimeError(
                f"Kafka {operation} failed for topic {topic_name!r} "
                f"(expected {expected_topic!r}), error_code={error_code}"
            )


class KafkaSmokeTopicLease:
    """Create, expose, and safely remove one isolated Kafka smoke topic."""

    def __init__(
        self,
        *,
        bootstrap_servers: str,
        auth_kwargs: Mapping[str, object],
        admin_factory: KafkaAdminFactory | None = None,
        metadata_timeout_seconds: float = 10.0,
        metadata_poll_interval_seconds: float = 0.25,
    ) -> None:
        if not bootstrap_servers.strip():
            raise ValueError("bootstrap_servers must be non-empty")
        self.identity = KafkaSmokeTopicIdentity()
        self._bootstrap_servers = bootstrap_servers
        self._auth_kwargs = dict(auth_kwargs)
        self._admin_factory = admin_factory or cast(
            KafkaAdminFactory,
            self._default_admin_factory,
        )
        self._metadata_timeout_seconds = metadata_timeout_seconds
        self._metadata_poll_interval_seconds = metadata_poll_interval_seconds
        self._admin: KafkaAdminClient | None = None
        self._created = False

    @staticmethod
    def _default_admin_factory(
        *,
        bootstrap_servers: str,
        **auth_kwargs: object,
    ) -> KafkaAdminClient:
        from aiokafka.admin import AIOKafkaAdminClient

        return cast(
            KafkaAdminClient,
            AIOKafkaAdminClient(
                bootstrap_servers=bootstrap_servers,
                **auth_kwargs,
            ),
        )

    @property
    def topic(self) -> str:
        """The unique topic allocated for this lease."""
        return self.identity.topic

    @property
    def group_id(self) -> str:
        """The unique consumer group allocated for this lease."""
        return self.identity.group_id

    async def __aenter__(self) -> KafkaSmokeTopicLease:
        """Start admin, create the topic, and wait for usable metadata."""
        self._admin = self._admin_factory(
            bootstrap_servers=self._bootstrap_servers,
            **self._auth_kwargs,
        )
        try:
            await self._admin.start()
            response = await self._admin.create_topics(
                [
                    NewTopic(
                        name=self.topic,
                        num_partitions=_PARTITION_COUNT,
                        replication_factor=_REPLICATION_FACTOR,
                    )
                ]
            )
            _assert_operation_succeeded(
                response,
                operation="create",
                expected_topic=self.topic,
            )
            self._created = True
            logger.info("kafka_smoke_topic_created topic=%s", self.topic)

            ready = await wait_for_topic_metadata(
                self._admin,
                self.topic,
                timeout_seconds=self._metadata_timeout_seconds,
                expected_partitions=_PARTITION_COUNT,
                poll_interval_seconds=self._metadata_poll_interval_seconds,
            )
            if not ready:
                raise TimeoutError(
                    f"Kafka topic metadata did not become ready: {self.topic!r}"
                )
            logger.info(
                "kafka_smoke_topic_metadata_ready topic=%s partitions=%d",
                self.topic,
                _PARTITION_COUNT,
            )
            return self
        except BaseException:
            try:
                await self._delete_created_topic()
            finally:
                await self._close_admin()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Delete only this lease's topic, then close its admin client."""
        _ = exc_type, exc_value, traceback
        try:
            await self._delete_created_topic()
        finally:
            await self._close_admin()

    async def _delete_created_topic(self) -> None:
        """Delete and verify absence of the topic owned by this lease."""
        if not self._created:
            return
        admin = self._admin
        if admin is None:
            raise RuntimeError("Kafka admin client missing for created topic")

        response = await admin.delete_topics([self.topic])
        _assert_operation_succeeded(
            response,
            operation="delete",
            expected_topic=self.topic,
        )
        deleted = await wait_for_topic_absence(
            admin,
            self.topic,
            timeout_seconds=self._metadata_timeout_seconds,
            poll_interval_seconds=self._metadata_poll_interval_seconds,
        )
        if not deleted:
            raise TimeoutError(
                f"Kafka topic deletion was not observable: {self.topic!r}"
            )
        self._created = False
        logger.info("kafka_smoke_topic_deleted topic=%s", self.topic)

    async def _close_admin(self) -> None:
        """Close the admin client once and release the local reference."""
        admin = self._admin
        self._admin = None
        if admin is not None:
            await admin.close()


__all__ = [
    "KafkaAdminClient",
    "KafkaAdminFactory",
    "KafkaSmokeTopic",
    "KafkaSmokeTopicIdentity",
    "KafkaSmokeTopicLease",
    "wait_for_topic_absence",
    "wait_for_topic_metadata",
]
