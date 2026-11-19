# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""One-shot HTTPS publisher for the runtime bus forward effect."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from math import isfinite
from typing import Literal, Protocol, cast

import httpx
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from omnimarket.nodes.node_runtime_bus_forward_effect.models.model_https_event_bus import (
    BATCH_OVERHEAD_BYTES,
    BATCH_PREFIX,
    BATCH_SUFFIX,
    ModelHttpsEventBus,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.models.model_https_ingest_response import (
    ModelHttpsIngestResponse,
    ModelIngestAcknowledgment,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.protocols import (
    PublisherRefusedError,
)

from ..handlers.https_deadline_transport import open_https_client, run_sync

_BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")
_NO_FULL_ATTEMPT = "flush deadline leaves no time for a full request attempt"


class _IngestContract(Protocol):
    """The shared ingest contract loaded when the HTTPS arm is selected."""

    CLIENT_HEADER_SET: Mapping[str, int | Literal["max_authorization_bytes"]]

    def canonical_topic(self, topic: str, /) -> str:
        raise NotImplementedError

    def compute_event_id(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: Iterable[tuple[str, bytes]],
        /,
    ) -> str:
        raise NotImplementedError


_INGEST_CONTRACT = cast(
    _IngestContract,
    import_module("omnibase_core.models.contracts.model_ingest_contract"),
)


@dataclass(frozen=True, slots=True)
class _Record:
    position: int
    event_id: str
    encoded: bytes


@dataclass(frozen=True, slots=True)
class _Attempt:
    acknowledged: tuple[int, ...] = ()
    reason: str | None = None
    retryable: bool = False
    may_have_published: bool = False


def _resolve_binding(reference: str, name: str, environ: Mapping[str, str]) -> str:
    if environ is os.environ:
        from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe

        secret = resolve_api_key_loop_safe(reference, required=False)
        value = secret.get_secret_value() if secret is not None else None
    else:
        env_name = reference.upper().replace(".", "_")
        value = environ.get(reference) or environ.get(env_name)
    if not value:
        raise PublisherRefusedError(f"{name} did not resolve")
    return value


def _https_url(value: str) -> httpx.URL:
    error = "ingest_url_ref must resolve to an absolute HTTPS route without credentials"
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        raise PublisherRefusedError(error) from None
    if (
        url.scheme != "https"
        or not url.host
        or url.path == "/"
        or url.userinfo
        or url.fragment
    ):
        raise PublisherRefusedError(error)
    return url


def _as_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _check_fields(
    config: ModelHttpsEventBus,
    topic: str,
    key: bytes | None,
    value: bytes,
    headers: Mapping[str, str],
) -> None:
    for field, size, limit, bound in (
        (
            "key",
            len(key) if key is not None else 0,
            "max_key_bytes",
            config.max_key_bytes,
        ),
        ("value", len(value), "max_value_bytes", config.max_value_bytes),
        ("headers", len(headers), "max_record_headers", config.max_record_headers),
        (
            "header name",
            max(map(len, headers), default=0),
            "max_header_name_chars",
            config.max_header_name_chars,
        ),
        (
            "header value",
            max(map(len, headers.values()), default=0),
            "max_header_value_chars",
            config.max_header_value_chars,
        ),
        ("topic", len(topic), "max_topic_chars", config.max_topic_chars),
    ):
        if size > bound:
            raise PublisherRefusedError(
                f"event-field-too-large: {field} exceeds {limit}"
            )


def _validate_headers(request: httpx.Request, max_authorization_bytes: int) -> None:
    declared = _INGEST_CONTRACT.CLIENT_HEADER_SET
    names = set(request.headers)
    undeclared = names - declared.keys()
    if undeclared:
        raise PublisherRefusedError(
            f"header-not-declared: {', '.join(sorted(undeclared))}"
        )
    if names != declared.keys() or len(request.headers.raw) != len(names):
        raise PublisherRefusedError("expected one of each declared header")
    for name, value in request.headers.raw:
        header = name.decode("ascii").lower()
        bound = declared[header]
        limit = max_authorization_bytes if bound == "max_authorization_bytes" else bound
        if len(value) > limit:
            raise PublisherRefusedError(f"header-too-large: {header}")


def _record_for_ack(
    records: Sequence[_Record], entry: ModelIngestAcknowledgment
) -> _Record | None:
    if entry.index >= len(records):
        return None
    record = records[entry.index]
    return record if record.event_id == entry.event_id else None


def _read_attempt(response: httpx.Response, pending: Sequence[_Record]) -> _Attempt:
    status = response.status_code
    if status == 202:
        return _Attempt(acknowledged=tuple(record.position for record in pending))

    retryable = status == 408 or 500 <= status < 600
    detail = f"HTTP {status}"
    try:
        reply = ModelHttpsIngestResponse.model_validate_json(response.content)
    except ValidationError:
        return _Attempt(
            reason=detail, retryable=retryable, may_have_published=500 <= status < 600
        )

    reasons = [reply.reason] if reply.reason else []
    acknowledged: list[int] = []
    uncertain = (
        500 <= status < 600
        and not isinstance(reply.published, tuple)
        and reply.reason != "door-busy"
    )
    if status == 422:
        reasons.extend(
            f"record {pending[entry.index].position} {entry.reason}"
            for entry in reply.refusals
            if entry.index < len(pending)
        )
    if status in (500, 503):
        if isinstance(reply.published, tuple):
            for entry in reply.published:
                if (record := _record_for_ack(pending, entry)) is not None:
                    acknowledged.append(record.position)
        for failure in reply.failed:
            if (record := _record_for_ack(pending, failure)) is not None:
                reasons.append(f"record {record.position} {failure.reason}")
                uncertain |= failure.reason == "delivery-unconfirmed"
        retryable &= all(failure.retryable for failure in reply.failed)
    return _Attempt(
        acknowledged=tuple(acknowledged),
        reason=f"{detail}: {', '.join(reasons)}" if reasons else detail,
        retryable=retryable and reply.retryable is not False,
        may_have_published=uncertain,
    )


class PublisherHttps:
    """Send bounded batches of recorded events to the endpoint selected by the overlay."""

    def __init__(
        self,
        config: ModelHttpsEventBus,
        *,
        url: str,
        token: str,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._url = _https_url(url)
        self._authorization = f"Bearer {token}"
        self._token_valid = _BEARER_TOKEN.fullmatch(token) is not None
        self._transport_factory = transport_factory
        self._clock = clock
        self._sleep = sleep
        self._records: list[_Record] = []
        self._publish_refusal: str | None = None
        self._failure: str | None = None

    @classmethod
    def from_overlay(
        cls, config: ModelHttpsEventBus, environ: Mapping[str, str]
    ) -> PublisherHttps:
        """Resolve the deployment bindings only when the forwarder chooses HTTPS."""
        return cls(
            config,
            url=_resolve_binding(config.ingest_url_ref, "ingest_url_ref", environ),
            token=_resolve_binding(config.auth_ref, "auth_ref", environ),
        )

    def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: dict[str, str]
    ) -> None:
        try:
            wire_topic = _INGEST_CONTRACT.canonical_topic(topic)
            if wire_topic != topic:
                raise PublisherRefusedError("topic-not-canonical")
            _check_fields(self._config, wire_topic, key, value, headers)
            event_id = _INGEST_CONTRACT.compute_event_id(
                wire_topic,
                key,
                value,
                ((name, text.encode("utf-8")) for name, text in headers.items()),
            )
            encoded = json.dumps(
                {
                    "topic": wire_topic,
                    "key": None if key is None else _as_base64(key),
                    "value": _as_base64(value),
                    "headers": headers,
                    "event_id": event_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > self._config.max_event_bytes:
                raise PublisherRefusedError("event-too-large: exceeds max_event_bytes")
        except (PydanticCustomError, UnicodeEncodeError) as exc:
            reason = (
                exc.type
                if isinstance(exc, PydanticCustomError)
                else "invalid UTF-8 record field"
            )
            self._publish_refusal = self._publish_refusal or reason
            raise PublisherRefusedError(reason) from None
        except PublisherRefusedError as exc:
            self._publish_refusal = self._publish_refusal or str(exc)
            raise
        self._records.append(_Record(len(self._records), event_id, encoded))

    def flush(self, timeout_s: float) -> int:
        records, self._records = self._records, []
        self._failure, self._publish_refusal = self._publish_refusal, None
        if not records:
            return 0
        deadline = self._clock() + timeout_s
        if not isfinite(deadline) or timeout_s <= 0:
            self._failure = _NO_FULL_ATTEMPT
            return len(records)
        if not self._token_valid:
            self._failure = "invalid authorization bearer token"
            return len(records)
        if len(self._authorization) > self._config.max_authorization_bytes:
            self._failure = (
                "header-too-large: authorization exceeds max_authorization_bytes"
            )
            return len(records)

        return run_sync(lambda: self._flush_records(records, deadline))

    def failure_detail(self) -> str | None:
        return self._publish_refusal or self._failure

    def _split(self, records: Sequence[_Record]) -> Iterator[list[_Record]]:
        batch: list[_Record] = []
        size = BATCH_OVERHEAD_BYTES
        for record in records:
            addition = len(record.encoded) + bool(batch)
            if (
                len(batch) == self._config.max_batch_records
                or size + addition > self._config.max_batch_bytes
            ):
                yield batch
                batch = []
                size = BATCH_OVERHEAD_BYTES
                addition = len(record.encoded)
            batch.append(record)
            size += addition
        if batch:
            yield batch

    def _flush_records(self, records: list[_Record], deadline: float) -> int:
        batches = list(self._split(records))
        unconfirmed = 0
        first_reason = None
        may_have_published = False
        with open_https_client(
            self._config.request_timeout_seconds, self._transport_factory
        ) as client:
            for index, batch in enumerate(batches):
                later = len(batches) - index - 1
                count, reason, uncertain = self._send_batch(
                    client, batch, deadline, later
                )
                if count:
                    unconfirmed += count
                    first_reason = first_reason or reason
                    may_have_published |= uncertain
        if first_reason is not None:
            self._failure = first_reason
        if may_have_published:
            self._failure = (
                f"{self._failure}; unconfirmed events may have been published"
            )
        return unconfirmed

    def _send_batch(
        self, client: httpx.Client, batch: list[_Record], deadline: float, later: int
    ) -> tuple[int, str | None, bool]:
        config = self._config
        budget = config.request_timeout_seconds
        reserve = later * config.recovery_budget_seconds
        acknowledged: set[int] = set()
        pending = batch
        reason = None
        may_have_published = False
        for attempt in range(config.max_attempts):
            if attempt:
                if (
                    self._clock() + config.retry_backoff_seconds + budget + reserve
                    > deadline
                ):
                    break
                self._sleep(config.retry_backoff_seconds)
            wall_started = time.monotonic()
            if self._clock() + budget + (reserve if attempt else 0) > deadline:
                break
            outcome = self._send_attempt(client, pending, wall_started + budget)
            acknowledged.update(outcome.acknowledged)
            pending = [
                record for record in batch if record.position not in acknowledged
            ]
            if not pending:
                return 0, None, False
            reason = reason or outcome.reason
            may_have_published |= outcome.may_have_published
            if not outcome.retryable:
                break
        return len(pending), reason or _NO_FULL_ATTEMPT, may_have_published

    def _send_attempt(
        self, client: httpx.Client, pending: list[_Record], deadline: float
    ) -> _Attempt:
        body = (
            BATCH_PREFIX
            + b",".join(record.encoded for record in pending)
            + BATCH_SUFFIX
        )
        # An explicit byte-body request supplies only Host and Content-Length
        # automatically. Client.build_request would merge four default headers.
        request = httpx.Request(
            "POST",
            self._url,
            headers={
                "authorization": self._authorization,
                "idempotency-key": pending[0].event_id,
                "content-type": "application/json",
            },
            content=body,
            extensions={"attempt_deadline": deadline},
        )
        _validate_headers(request, self._config.max_authorization_bytes)
        try:
            response = client.send(request, auth=None, follow_redirects=False)
        except (httpx.RequestError, TimeoutError) as exc:
            reason = (
                "wall-clock attempt deadline"
                if isinstance(exc, TimeoutError)
                else type(exc).__name__
            )
            return _Attempt(reason=reason, retryable=True, may_have_published=True)
        return _read_attempt(response, pending)
