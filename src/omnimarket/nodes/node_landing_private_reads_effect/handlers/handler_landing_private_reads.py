# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""List the staged landing controller copy's reads of private clones and overlays (rebuild piece 9, day one).

Pure over the files it is given. A line that names a private root is a ``read`` when it matches one of the
overlay's read-context patterns (a path, an import, an environment lookup) and is not a comment or a remote
repository slug; otherwise it is ``name_only`` and listed but not counted. Each (file, root) pair with a read
must be declared in the overlay with its destination. The reading is WORKING when at least one file was
scanned and no read is undeclared, and NOT_WORKING otherwise; the result carries the event to publish.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Final, Protocol

from omnimarket.nodes.node_landing_private_reads_effect.models.model_landing_private_reads import (
    EnumFailureCode,
    EnumReadKind,
    EnumVerdict,
    ModelFileRoot,
    ModelLandingPrivateReadsOverlay,
    ModelLandingPrivateReadsRequest,
    ModelLandingPrivateReadsResult,
    ModelListedRead,
    ModelPrivateRead,
    ModelPrivateReadsEvent,
)

#: Declared in contract.yaml ``event_bus.publish_topics``.
TOPIC_LISTED: Final = "onex.evt.omnimarket.landing-private-reads-listed.v1"


class ProtocolPrivateReadsEventSink(Protocol):
    def publish(self, event: ModelPrivateReadsEvent) -> None: ...


class InMemoryPrivateReadsEventSink:
    """The default sink: keeps every published event in order."""

    def __init__(self) -> None:
        self.events: list[ModelPrivateReadsEvent] = []

    def publish(self, event: ModelPrivateReadsEvent) -> None:
        self.events.append(event)


def _token_pattern(token: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(token) + r"(?![A-Za-z0-9_\-])")


def classify_line(
    line: str,
    start: int,
    overlay: ModelLandingPrivateReadsOverlay,
    contexts: list[re.Pattern[str]],
) -> EnumReadKind:
    """Return ``read`` or ``name_only`` for a root word found at ``start`` in ``line``."""
    if any(line.lstrip().startswith(prefix) for prefix in overlay.comment_prefixes):
        return "name_only"
    if any(line[:start].endswith(prefix) for prefix in overlay.slug_prefixes):
        return "name_only"
    if any(re.search(pattern, line) for pattern in overlay.name_only_patterns):
        return "name_only"
    if any(pattern.search(line) for pattern in contexts):
        return "read"
    return "name_only"


def scan(request: ModelLandingPrivateReadsRequest) -> list[ModelPrivateRead]:
    overlay = request.overlay
    contexts = [re.compile(p) for p in overlay.read_context_patterns]
    tokens = [
        (root.name, token, _token_pattern(token))
        for root in overlay.roots
        for token in root.tokens
    ]
    found: list[ModelPrivateRead] = []
    for source in sorted(request.sources, key=lambda s: s.path):
        for number, line in enumerate(source.text.splitlines(), start=1):
            seen_roots: set[str] = set()
            for root_name, token, pattern in tokens:
                if root_name in seen_roots:
                    continue
                match = pattern.search(line)
                if match is None:
                    continue
                seen_roots.add(root_name)
                found.append(
                    ModelPrivateRead(
                        file=source.path,
                        line=number,
                        root=root_name,
                        token=token,
                        kind=classify_line(line, match.start(), overlay, contexts),
                    )
                )
    return found


class HandlerLandingPrivateReads:
    """``handle(request) -> result``; publishes one event per listing to the injected sink."""

    def __init__(self, sink: ProtocolPrivateReadsEventSink | None = None) -> None:
        self.sink: ProtocolPrivateReadsEventSink = (
            sink or InMemoryPrivateReadsEventSink()
        )

    def handle(
        self, request: ModelLandingPrivateReadsRequest
    ) -> ModelLandingPrivateReadsResult:
        reads = scan(request)
        declared = {(d.file, d.root): d.destination for d in request.overlay.declared}
        lines: dict[tuple[str, str], list[int]] = defaultdict(list)
        for read in reads:
            if read.kind == "read":
                lines[(read.file, read.root)].append(read.line)
        listed = [
            ModelListedRead(file=f, root=r, lines=ln, destination=declared.get((f, r)))
            for (f, r), ln in sorted(lines.items())
        ]
        undeclared = [
            ModelFileRoot(file=x.file, root=x.root)
            for x in listed
            if x.destination is None
        ]
        stale = [
            ModelFileRoot(file=f, root=r)
            for (f, r) in sorted(declared)
            if (f, r) not in lines
        ]
        by_root = Counter(read.root for read in reads if read.kind == "read")
        files_scanned = len(request.sources)
        failure: EnumFailureCode | None
        if files_scanned == 0:
            failure = "no_sources"
        elif undeclared:
            failure = "undeclared_private_read"
        else:
            failure = None
        verdict: EnumVerdict = "WORKING" if failure is None else "NOT_WORKING"
        event = ModelPrivateReadsEvent(
            topic=TOPIC_LISTED,
            correlation_id=request.correlation_id,
            verdict=verdict,
            failure_code=failure,
            files_scanned=files_scanned,
            reads=sum(by_root.values()),
            undeclared=len(undeclared),
        )
        self.sink.publish(event)
        return ModelLandingPrivateReadsResult(
            correlation_id=request.correlation_id,
            files_scanned=files_scanned,
            reads=reads,
            listed=listed,
            undeclared=undeclared,
            stale_declared=stale,
            reads_by_root=dict(sorted(by_root.items())),
            verdict=verdict,
            failure_code=failure,
            event=event,
        )


__all__ = [
    "TOPIC_LISTED",
    "HandlerLandingPrivateReads",
    "InMemoryPrivateReadsEventSink",
    "ProtocolPrivateReadsEventSink",
    "classify_line",
    "scan",
]
