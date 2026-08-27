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
from types import SimpleNamespace
from typing import Protocol, cast
from uuid import uuid4

from aiokafka.admin import NewTopic
from aiokafka.errors import KafkaError, UnknownTopicOrPartitionError
from aiokafka.protocol.admin import DeleteGroupsRequest

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

    async def list_consumer_groups(self) -> object:
        """Return the broker's consumer-group census."""

    async def delete_consumer_groups(self, groups: list[str]) -> object:
        """Delete exactly the supplied, inactive consumer groups."""


KafkaAdminFactory = Callable[..., KafkaAdminClient]


class _AIOKafkaAdminClientLike(Protocol):
    """Operations used from the concrete aiokafka admin client."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def create_topics(self, topics: list[NewTopic]) -> object: ...

    async def delete_topics(self, topics: list[str]) -> object: ...

    async def describe_topics(self, topics: list[str]) -> object: ...

    async def list_consumer_groups(self) -> object: ...

    async def find_coordinator(self, group_id: str) -> int: ...

    async def _send_request(
        self,
        request: DeleteGroupsRequest,
        node_id: int,
    ) -> object: ...


class KafkaSmokeTopic(Protocol):
    """Typed topic/group shape consumed by the integration smoke test."""

    @property
    def topic(self) -> str:
        """The topic allocated for the test."""

    @property
    def group_id(self) -> str:
        """The consumer group allocated for the test."""

    @property
    def effective_group_id(self) -> str | None:
        """The exact broker group used after bus-specific scoping."""

    def bind_effective_group_id(self, group_id: str) -> None:
        """Record the exact group that this lease is authorized to remove."""


class KafkaSmokeTopicIdentity:
    """Unique topic and consumer group allocated for one smoke-test run."""

    __slots__ = ("group_id", "topic")

    def __init__(self, *, suffix: str | None = None) -> None:
        run_suffix = suffix or uuid4().hex
        self.topic = f"{KAFKA_SMOKE_TOPIC_PREFIX}-{run_suffix}.v1"
        self.group_id = f"{_GROUP_PREFIX}-{run_suffix}"


def topic_scoped_consumer_group_id(*, group_id: str, topic: str) -> str:
    """Return EventBusKafka's explicit-group topic scope.

    The integration test resolves the complete value through EventBusKafka so
    configured instance discrimination and length handling remain canonical.
    This small helper is useful for unit fixtures and documents the required
    ``.__t.`` suffix without duplicating those optional transformations.
    """
    return f"{group_id}.__t.{topic}"


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


def _group_name(entry: object) -> str:
    """Extract a group ID from aiokafka's tuple or object census entry."""
    if isinstance(entry, Mapping):
        raw_group = entry.get("group_id", entry.get("group"))
    elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes, bytearray)):
        if not entry:
            raise TypeError("Kafka consumer-group entry is empty")
        raw_group = entry[0]
    else:
        raw_group = _field(entry, "group_id")

    if not isinstance(raw_group, str) or not raw_group:
        raise TypeError(f"Kafka consumer-group name must be non-empty: {raw_group!r}")
    return raw_group


def consumer_group_names(response: object) -> tuple[str, ...]:
    """Return an exact, value-safe census of consumer-group IDs."""
    entries = response
    if isinstance(response, Mapping):
        entries = response.get("groups")
    else:
        groups = _field(response, "groups")
        if groups is not None:
            entries = groups

    if not isinstance(entries, Sequence) or isinstance(
        entries, (str, bytes, bytearray)
    ):
        raise TypeError(
            "Kafka consumer-group census must be a sequence; "
            f"got {type(entries).__name__}"
        )
    return tuple(_group_name(entry) for entry in entries)


async def wait_for_consumer_group_absence(
    admin_client: KafkaAdminClient,
    group_id: str,
    *,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.25,
) -> bool:
    """Poll until the exact consumer group is absent from broker census."""
    if not group_id.strip():
        raise ValueError("group_id must be non-empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            groups = consumer_group_names(await admin_client.list_consumer_groups())
            if group_id not in groups:
                return True
        except (KafkaError, OSError):
            pass

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_interval_seconds, remaining))


def _response_entries(
    response: object,
    operation: str,
    attributes: tuple[str, ...],
) -> Sequence[object]:
    """Extract a version-compatible result sequence from an admin response."""
    for attribute in attributes:
        entries = _field(response, attribute)
        if entries is not None:
            if isinstance(entries, Sequence) and not isinstance(
                entries, (str, bytes, bytearray)
            ):
                return entries
            raise TypeError(
                f"Kafka {operation} response field {attribute!r} must be a sequence"
            )
    raise TypeError(
        f"Kafka {operation} response has no supported result field; "
        f"got {type(response).__name__}"
    )


def _entry_error(entry: object, resource: str) -> tuple[str, int]:
    """Extract a named resource and integer error code from a response entry."""
    if isinstance(entry, Mapping):
        name_key = "topic" if resource == "topic" else "group_id"
        raw_name = entry.get(name_key)
        if resource == "consumer-group" and raw_name is None:
            raw_name = entry.get("group")
        raw_code = entry.get("error_code")
    elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes, bytearray)):
        if len(entry) < 2:
            raise TypeError(f"Kafka {resource}-result entry is too short: {entry!r}")
        raw_name = entry[0]
        raw_code = entry[1]
    else:
        name_key = "topic" if resource == "topic" else "group_id"
        raw_name = _field(entry, name_key)
        if resource == "consumer-group" and raw_name is None:
            raw_name = _field(entry, "group")
        raw_code = _field(entry, "error_code")

    if not isinstance(raw_name, str):
        raise TypeError(f"Kafka {resource}-result name must be a string: {raw_name!r}")
    if isinstance(raw_code, bool) or not isinstance(raw_code, int):
        raise TypeError(
            f"Kafka {resource}-result code must be an integer: {raw_code!r}"
        )
    return raw_name, raw_code


def _assert_operation_succeeded(
    response: object,
    *,
    operation: str,
    expected_topic: str,
) -> None:
    """Fail closed unless the response is exactly one successful topic entry."""
    attributes = (
        ("topic_errors", "topic_error_codes")
        if operation == "create"
        else ("topic_error_codes", "topic_errors")
    )
    entries = _response_entries(response, f"topic {operation}", attributes)
    if not entries:
        raise RuntimeError(
            f"Kafka {operation} returned no topic result for expected "
            f"{expected_topic!r}"
        )

    names: list[str] = []
    for entry in entries:
        topic_name, error_code = _entry_error(entry, "topic")
        names.append(topic_name)
        if topic_name != expected_topic:
            raise RuntimeError(
                f"Kafka {operation} returned unexpected topic {topic_name!r}; "
                f"expected exactly {expected_topic!r}"
            )
        if names.count(topic_name) > 1:
            raise RuntimeError(
                f"Kafka {operation} returned duplicate topic entry {topic_name!r}"
            )
        if error_code != 0:
            raise RuntimeError(
                f"Kafka {operation} failed for topic {topic_name!r} "
                f"(expected {expected_topic!r}), error_code={error_code}"
            )

    if names != [expected_topic]:
        raise RuntimeError(
            f"Kafka {operation} result did not contain exactly one entry for "
            f"{expected_topic!r}: {names!r}"
        )


def _assert_group_operation_succeeded(
    response: object,
    *,
    expected_group: str,
) -> None:
    """Fail closed unless DeleteGroups returned exactly one successful group."""
    entries = _response_entries(
        response,
        "consumer-group delete",
        ("results", "group_error_codes"),
    )
    if not entries:
        raise RuntimeError(
            "Kafka consumer-group delete returned no result for expected "
            f"{expected_group!r}"
        )

    names: list[str] = []
    for entry in entries:
        group_name, error_code = _entry_error(entry, "consumer-group")
        names.append(group_name)
        if group_name != expected_group:
            raise RuntimeError(
                "Kafka consumer-group delete returned unexpected group "
                f"{group_name!r}; expected exactly {expected_group!r}"
            )
        if names.count(group_name) > 1:
            raise RuntimeError(
                "Kafka consumer-group delete returned duplicate group entry "
                f"{group_name!r}"
            )
        if error_code != 0:
            raise RuntimeError(
                "Kafka consumer-group delete failed for group "
                f"{group_name!r}, error_code={error_code}"
            )

    if names != [expected_group]:
        raise RuntimeError(
            "Kafka consumer-group delete result did not contain exactly one "
            f"entry for {expected_group!r}: {names!r}"
        )


class _AIOKafkaAdminClientAdapter:
    """Typed admin seam adding DeleteGroups to aiokafka's public admin API.

    aiokafka 0.14 exposes ``list_consumer_groups`` but not a corresponding
    high-level delete method.  DeleteGroups is nevertheless part of its
    negotiated protocol, so this adapter keeps the raw request confined to the
    lifecycle seam instead of switching the test to a second client or shell
    command.  One group is sent per request because each group can have a
    different coordinator.
    """

    def __init__(self, client: object) -> None:
        self._client = client

    async def start(self) -> None:
        client = cast(_AIOKafkaAdminClientLike, self._client)
        await client.start()

    async def close(self) -> None:
        client = cast(_AIOKafkaAdminClientLike, self._client)
        await client.close()

    async def create_topics(self, topics: list[NewTopic]) -> object:
        client = cast(_AIOKafkaAdminClientLike, self._client)
        return await client.create_topics(topics)

    async def delete_topics(self, topics: list[str]) -> object:
        client = cast(_AIOKafkaAdminClientLike, self._client)
        return await client.delete_topics(topics)

    async def describe_topics(self, topics: list[str]) -> object:
        client = cast(_AIOKafkaAdminClientLike, self._client)
        return await client.describe_topics(topics)

    async def list_consumer_groups(self) -> object:
        client = cast(_AIOKafkaAdminClientLike, self._client)
        return await client.list_consumer_groups()

    async def delete_consumer_groups(self, groups: list[str]) -> object:
        if not groups or any(not group.strip() for group in groups):
            raise ValueError("groups must contain non-empty IDs")

        client = cast(_AIOKafkaAdminClientLike, self._client)

        results: list[object] = []
        for group_id in groups:
            coordinator_id = await client.find_coordinator(group_id)
            response = await client._send_request(
                DeleteGroupsRequest([group_id]),
                coordinator_id,
            )
            raw_results = _field(response, "results")
            if not isinstance(raw_results, Sequence) or isinstance(
                raw_results, (str, bytes, bytearray)
            ):
                raise TypeError(
                    "Kafka DeleteGroups response results must be a sequence"
                )
            results.extend(raw_results)
        return SimpleNamespace(results=results)


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
        self._effective_group_id: str | None = None

    @staticmethod
    def _default_admin_factory(
        *,
        bootstrap_servers: str,
        **auth_kwargs: object,
    ) -> KafkaAdminClient:
        from aiokafka.admin import AIOKafkaAdminClient

        return cast(
            KafkaAdminClient,
            _AIOKafkaAdminClientAdapter(
                AIOKafkaAdminClient(
                    bootstrap_servers=bootstrap_servers,
                    **auth_kwargs,
                )
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

    @property
    def effective_group_id(self) -> str | None:
        """The exact broker group registered by the consuming bus."""
        return self._effective_group_id

    def bind_effective_group_id(self, group_id: str) -> None:
        """Authorize one exact topic-scoped group for later cleanup.

        The lease never discovers or deletes groups by prefix.  Callers must
        provide the exact group computed by the bus, including its topic scope;
        rejecting a different shape prevents an accidental shared-group delete.
        """
        expected_suffix = f".__t.{self.topic}"
        if not group_id.strip():
            raise ValueError("effective consumer group ID must be non-empty")
        if not group_id.startswith(self.group_id) or not group_id.endswith(
            expected_suffix
        ):
            raise ValueError(
                "effective consumer group ID must be this lease's topic-scoped "
                f"group: expected prefix {self.group_id!r} and suffix "
                f"{expected_suffix!r}"
            )
        if (
            self._effective_group_id is not None
            and self._effective_group_id != group_id
        ):
            raise ValueError(
                "effective consumer group ID is already bound to "
                f"{self._effective_group_id!r}"
            )
        self._effective_group_id = group_id

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
        except BaseException as primary_error:
            cleanup_failures = await self._cleanup()
            _handle_cleanup_failures(primary_error, cleanup_failures)
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Delete only this lease's resources, preserving any body failure."""
        _ = exc_type, traceback
        cleanup_failures = await self._cleanup()
        _handle_cleanup_failures(exc_value, cleanup_failures)

    async def _cleanup(self) -> list[tuple[str, BaseException]]:
        """Run every cleanup phase and retain each failure for deterministic handling."""
        failures: list[tuple[str, BaseException]] = []
        operations: tuple[tuple[str, Callable[[], Awaitable[None]]], ...] = (
            ("consumer-group", self._delete_effective_consumer_group),
            ("topic", self._delete_created_topic),
            ("admin-close", self._close_admin),
        )
        for label, operation in operations:
            try:
                await operation()
            except BaseException as error:
                failures.append((label, error))
        return failures

    async def _delete_effective_consumer_group(self) -> None:
        """Delete and verify absence of only the bound effective group."""
        group_id = self._effective_group_id
        if group_id is None:
            return
        admin = self._admin
        if admin is None:
            raise RuntimeError("Kafka admin client missing for bound consumer group")

        census = consumer_group_names(await admin.list_consumer_groups())
        was_present = group_id in census
        if was_present:
            response = await admin.delete_consumer_groups([group_id])
            _assert_group_operation_succeeded(
                response,
                expected_group=group_id,
            )

        absent = await wait_for_consumer_group_absence(
            admin,
            group_id,
            timeout_seconds=self._metadata_timeout_seconds,
            poll_interval_seconds=self._metadata_poll_interval_seconds,
        )
        if not absent:
            raise TimeoutError(
                f"Kafka consumer-group deletion was not observable: {group_id!r}"
            )
        logger.info(
            "kafka_smoke_consumer_group_absent group=%s was_present=%s",
            group_id,
            was_present,
        )
        self._effective_group_id = None

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


def _handle_cleanup_failures(
    primary_error: BaseException | None,
    failures: Sequence[tuple[str, BaseException]],
) -> None:
    """Preserve a primary error while making every cleanup failure visible."""
    if not failures:
        return
    if primary_error is not None:
        _attach_cleanup_failures(primary_error, failures)
        return

    first_label, first_error = failures[0]
    for label, error in failures[1:]:
        first_error.add_note(_cleanup_failure_note(label, error))
    first_error.add_note(f"first cleanup failure phase: {first_label}")
    raise first_error


def _cleanup_failure_note(label: str, error: BaseException) -> str:
    """Render one deterministic cleanup failure note without message payloads."""
    return f"Kafka smoke cleanup failed during {label}: {type(error).__name__}: {error}"


def _attach_cleanup_failures(
    primary_error: BaseException,
    failures: Sequence[tuple[str, BaseException]],
) -> None:
    """Attach cleanup failures to the active primary exception in order."""
    for label, error in failures:
        primary_error.add_note(_cleanup_failure_note(label, error))


async def cleanup_kafka_smoke_consumer(
    unsubscribe: Callable[[], Awaitable[None]],
    consuming_task: asyncio.Task[None] | None,
    *,
    primary_error: BaseException | None,
) -> None:
    """Unsubscribe and stop the consumer without masking a primary failure."""
    failures: list[tuple[str, BaseException]] = []
    try:
        await unsubscribe()
    except BaseException as error:
        failures.append(("unsubscribe", error))

    if consuming_task is not None:
        consuming_task.cancel()
        try:
            await consuming_task
        except asyncio.CancelledError:
            pass
        except BaseException as error:
            failures.append(("consumer-task", error))

    _handle_cleanup_failures(primary_error, failures)


__all__ = [
    "KafkaAdminClient",
    "KafkaAdminFactory",
    "KafkaSmokeTopic",
    "KafkaSmokeTopicIdentity",
    "KafkaSmokeTopicLease",
    "cleanup_kafka_smoke_consumer",
    "consumer_group_names",
    "topic_scoped_consumer_group_id",
    "wait_for_consumer_group_absence",
    "wait_for_topic_absence",
    "wait_for_topic_metadata",
]
