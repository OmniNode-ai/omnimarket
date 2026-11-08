# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerJobStartSource: read which jobs started in a window, judge the read, keep it (OMN-22897).

EFFECT handler: reads only, and appends one record per read to the retained store the overlay names.
The reader and the store are injected, so a fake reader runs the whole chain with no network. A read
that cannot be completed never raises and never becomes zero starts: it carries a typed failure, the
reading is UNKNOWN on every subject, and the run ends in the failure topic (``event_for``).

The reading has three subjects:

- ``js_repositories_read``: every declared repository was read to its end;
- ``js_read_age``: the read is no older than the overlay's max age;
- ``js_public_starts_off_baseline``: no job of public origin (a fork head, or a public repository)
  started on a watched runner group unless its repository is on the dated baseline; a fork never is.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_job_start_source_effect.adapters.github_job_start_reader import (
    GithubJobStartReader,
)
from omnimarket.nodes.node_job_start_source_effect.adapters.inmemory_job_start_reader import (
    InMemoryJobStartReader,
)
from omnimarket.nodes.node_job_start_source_effect.adapters.job_start_store import (
    InMemoryJobStartStore,
    LedgerJobStartStore,
)
from omnimarket.nodes.node_job_start_source_effect.handlers.http_transport import (
    UrllibHttpTransport,
)
from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    EnumJobStartFailure,
    JobStartReaderError,
    ModelJobStart,
    ModelJobStartRead,
    ModelJobStartReading,
    ModelJobStartRequest,
    ModelJobStartResult,
    ModelJobStartSubject,
)
from omnimarket.nodes.node_job_start_source_effect.overlay import (
    ModelInMemorySource,
    ModelJobStartOverlay,
    ModelJobStartSettings,
    ModelLedgerStore,
    load_overlay,
)
from omnimarket.nodes.node_job_start_source_effect.protocols.protocol_job_start import (
    ProtocolHttpGet,
    ProtocolJobStartReader,
    ProtocolJobStartStore,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
SUBJECT_REPOSITORIES = "js_repositories_read"
SUBJECT_AGE = "js_read_age"
SUBJECT_PUBLIC = "js_public_starts_off_baseline"


def load_topics() -> dict[str, str]:
    """The topics by role, read from the contract (the handler holds no topic string)."""
    return dict(yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["topics"])


def load_operation_timeout_seconds() -> float:
    """The operation timeout the contract declares (``descriptor.timeout_ms``), in seconds.

    The whole read, every call included, must end inside it; the handler holds no duration of its own.
    """
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return float(document["descriptor"]["timeout_ms"]) / 1000


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_public_off_baseline(
    start: ModelJobStart, settings: ModelJobStartSettings
) -> bool:
    """A start of public origin on a watched runner group, off the dated baseline."""
    if start.runner_group_name not in settings.watched_runner_groups:
        return False
    if start.head_is_fork:
        return True
    return (
        not start.repository_private
        and start.repository not in settings.baseline_public_repositories
    )


def _unknown(reason: str) -> ModelJobStartReading:
    return ModelJobStartReading(
        verdict=EnumReadingVerdict.UNKNOWN,
        subjects=tuple(
            ModelJobStartSubject(
                name=name, verdict=EnumReadingVerdict.UNKNOWN, reason=reason
            )
            for name in (SUBJECT_REPOSITORIES, SUBJECT_AGE, SUBJECT_PUBLIC)
        ),
    )


def judge(
    read: ModelJobStartRead,
    failure: EnumJobStartFailure | None,
    declared: tuple[str, ...],
    settings: ModelJobStartSettings,
    *,
    age_seconds: float,
) -> ModelJobStartReading:
    if failure is not None:
        return _unknown(failure.value)
    if age_seconds > settings.max_age_seconds:
        return _unknown("read-stale")
    public = tuple(s for s in read.starts if is_public_off_baseline(s, settings))
    subjects = (
        ModelJobStartSubject(
            name=SUBJECT_REPOSITORIES,
            verdict=EnumReadingVerdict.WORKING,
            count=len(declared),
        ),
        ModelJobStartSubject(name=SUBJECT_AGE, verdict=EnumReadingVerdict.WORKING),
        ModelJobStartSubject(
            name=SUBJECT_PUBLIC,
            verdict=EnumReadingVerdict.NOT_WORKING
            if public
            else EnumReadingVerdict.WORKING,
            reason="public-start-on-watched-group" if public else "",
            count=len(public),
            run_ids=tuple(dict.fromkeys(s.run_id for s in public)),
        ),
    )
    verdict = (
        EnumReadingVerdict.NOT_WORKING
        if any(s.verdict is EnumReadingVerdict.NOT_WORKING for s in subjects)
        else EnumReadingVerdict.WORKING
    )
    return ModelJobStartReading(verdict=verdict, subjects=subjects)


class HandlerJobStartSource:
    def __init__(
        self,
        *,
        overlay: ModelJobStartOverlay | None = None,
        reader: ProtocolJobStartReader | None = None,
        store: ProtocolJobStartStore | None = None,
        http_get: ProtocolHttpGet | None = None,
        appender: ProtocolReadingAppender | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._overlay = overlay
        self._reader = reader
        self._store = store
        self._http_get = http_get
        self._appender = appender
        self._environ = environ
        self._clock = clock

    def _reader_for(self, overlay: ModelJobStartOverlay) -> ProtocolJobStartReader:
        if self._reader is not None:
            return self._reader
        source = overlay.source
        if isinstance(source, ModelInMemorySource):
            return InMemoryJobStartReader(source.fixture)
        return GithubJobStartReader(
            source,
            self._http_get or UrllibHttpTransport(),
            os.environ if self._environ is None else self._environ,
            run_lookback_seconds=overlay.settings.run_lookback_seconds,
            operation_timeout_seconds=load_operation_timeout_seconds(),
            clock=self._clock or time.monotonic,
        )

    def _store_for(self, overlay: ModelJobStartOverlay) -> ProtocolJobStartStore:
        if self._store is not None:
            return self._store
        setting = overlay.store
        if isinstance(setting, ModelLedgerStore):
            environ = os.environ if self._environ is None else self._environ
            host = environ.get(setting.ledger_append.host_env, "").strip()
            return LedgerJobStartStore(self._appender, setting, host)
        self._store = InMemoryJobStartStore()
        return self._store

    def handle(self, request: ModelJobStartRequest) -> ModelJobStartResult:
        overlay = self._overlay or load_overlay(self._environ)
        settings = overlay.settings
        declared = settings.repositories
        since = request.now - timedelta(seconds=settings.window_seconds)
        try:
            read = self._reader_for(overlay).read(
                repositories=declared, since=since, until=request.now, now=request.now
            )
        except JobStartReaderError as stop:
            read = ModelJobStartRead(
                read_at=request.now,
                calls=stop.calls,
                read_failure=stop.failure,
                failed_repository=stop.repository,
            )
        except Exception:
            # Whatever the reader raised, the run reports a typed failure; the message is not kept
            # because it can carry a URL or a token.
            read = ModelJobStartRead(
                read_at=request.now,
                calls=0,
                read_failure=EnumJobStartFailure.READER_RAISED,
            )
        failure = read.read_failure
        failed_repository = read.failed_repository
        if failure is None:
            missing = [r for r in declared if r not in read.repositories_read]
            if missing:
                failure = EnumJobStartFailure.REPOSITORIES_INCOMPLETE
                failed_repository = missing[0]
        starts = (
            ()
            if failure is not None
            else tuple(
                sorted(
                    {s.job_id: s for s in read.starts}.values(),
                    key=lambda s: (s.started_at, s.run_id, s.job_id),
                )
            )
        )
        reading = judge(
            read.model_copy(update={"starts": starts}),
            failure,
            declared,
            settings,
            age_seconds=(request.now - read.read_at).total_seconds(),
        )
        # The full-precision time and window: stamp_text keeps whole seconds, so two reads in one second collided.
        row_key = _sha(
            f"{request.now.isoformat()}|{since.isoformat()}|{overlay.overlay_hash()}"
        )[:32]
        result = ModelJobStartResult(
            correlation_id=request.correlation_id,
            backend=overlay.source.backend,
            calls=read.calls,
            window_start=since,
            window_end=request.now,
            read_at=read.read_at,
            declared_repositories=declared,
            repositories_read=() if failure is not None else read.repositories_read,
            starts=starts,
            read_failure=failure,
            failed_repository=failed_repository,
            reading=reading,
            row_key=row_key,
        )
        return result.model_copy(
            update={"store": self._store_for(overlay).append(result)}
        )

    @staticmethod
    def event_for(result: ModelJobStartResult) -> tuple[str, dict[str, Any]]:
        """The terminal event of a run: a failure topic when the read or the store failed."""
        topics = load_topics()
        if result.read_failure is not None:
            topic = topics["read_failed"]
        elif result.store is not None and result.store.failed:
            topic = topics["store_failed"]
        else:
            topic = topics["read_completed"]
        body = result.model_dump(mode="json", exclude={"starts"})
        body["job_start_count"] = result.job_start_count
        return topic, body


__all__ = [
    "SUBJECT_AGE",
    "SUBJECT_PUBLIC",
    "SUBJECT_REPOSITORIES",
    "HandlerJobStartSource",
    "is_public_off_baseline",
    "judge",
    "load_operation_timeout_seconds",
    "load_topics",
]
