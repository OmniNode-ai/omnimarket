# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grade the call recorder's lifecycle against the Endpoint Security observer (OMN-23204).

``handle(ModelLifecycleControlsRequest) -> ModelLifecycleControlsResult``. A pure function of the
text the caller supplies: it reads no file, clock, network or environment. Every rule is keyed on
the execution identity (pid, pidversion), never on a pid alone, and every time is compared in
nanoseconds, converted with the timebase of whoever read the ticks.

* The observer stream must be one unbroken run of ``global_seq_num``; a gap, or no event at all,
  makes the whole leg ``lifecycle-unobserved``.
* The observer decides which identities belong to a tree: the exec target that opens it, the
  targets of later execs by a member, the children of forks by a member; an exit closes one.
* Every exec of a member needs a recorder ``load`` line for that exact identity and image, timed
  after the exec and before the identity's next exec or exit (``recorder-blind-image``).
* Every fork of a member needs a recorder ``fork`` call and ``child`` line, or a ``posix_spawn``
  call from the parent and the child's next exec (``recorder-missed-fork``).
* A control tree is built so that the recorder cannot see it; it passes only by reading blind.
* The graded tree's own controls: a forked child's rejected connect, a rejected connect with no
  packet on the wire, and a lookup served twice from the cache with no query on the wire.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Final

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_walk_lifecycle_controls_compute.models.model_lifecycle_controls import (
    ModelControlOutcome,
    ModelLifecycleControlsRequest,
    ModelLifecycleControlsResult,
    ModelLifecycleControlsSettings,
    ModelTreeInput,
    ModelTreeResult,
)

#: The overlay id of this node's settings: <root>/<id>/overlay.yaml.
LIFECYCLE_CONTROLS_OVERLAY_ID = "node_walk_lifecycle_controls_compute"

REASON_UNOBSERVED = "lifecycle-unobserved"
REASON_CLOCK_UNDECLARED = "recorder-clock-undeclared"
REASON_BLIND_IMAGE = "recorder-blind-image"
REASON_MISSED_FORK = "recorder-missed-fork"
REASON_BLIND_CHILD = "recorder-blind-child"
REASON_CONTROL_FAILED = "coverage-control-failed"

CONTROL_FORKED_CHILD = "forked_child"
CONTROL_REJECTED_CONNECT = "rejected_connect_recorded"
CONTROL_CACHED_LOOKUP = "cache_served_lookup"
CONTROL_BLIND_IMAGE = "blind_image_control"

_EVENT_EXEC: Final = 9
_EVENT_FORK: Final = 11
_EVENT_EXIT: Final = 15

_FORK_CALLS: Final = frozenset({"fork", "vfork"})
_SPAWN_CALLS: Final = frozenset({"posix_spawn", "posix_spawnp"})
_LOOKUP_CALLS: Final = frozenset({"getaddrinfo", "gethostbyname", "gethostbyname2"})
_LOOKUP_PREFIX: Final = "DNSService"
_MIN_CACHED_LOOKUPS: Final = 2

Ident = tuple[int, int]


def load_lifecycle_controls_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelLifecycleControlsSettings:
    """The controls' settings from the first overlay root; none supplied is refused."""
    return load_structured_overlay(
        LIFECYCLE_CONTROLS_OVERLAY_ID, ModelLifecycleControlsSettings, environ
    )


@dataclass(frozen=True)
class _Event:
    seq: int
    kind: int
    ns: int
    who: Ident
    # The exec target or the fork child; unused for an exit.
    other: Ident | None
    path: str


@dataclass(frozen=True)
class _Line:
    kind: str
    call: str
    who: Ident
    image: str
    ns: int
    detail: str


@dataclass
class _TreeRead:
    reasons: set[str] = field(default_factory=set)
    exec_total: int = 0
    exec_met: int = 0
    fork_events: int = 0
    # Fork children reconciled through a fork call and a child line.
    reconciled_children: set[Ident] = field(default_factory=set)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _token(value: object) -> Ident | None:
    if not isinstance(value, dict):
        return None
    pid = _as_int(value.get("pid"))
    version = _as_int(value.get("pidversion"))
    if pid is None or version is None:
        return None
    return (pid, version)


def _ns(ticks: int, numer: int, denom: int) -> int:
    return ticks * numer // denom


def _normalise_path(path: str) -> str:
    return path[len("/private") :] if path.startswith("/private/") else path


def _from_first_brace(line: str) -> str | None:
    start = line.find("{")
    return None if start < 0 else line[start:]


def _parse_event(row: dict[str, object], numer: int, denom: int) -> _Event | None:
    seq = _as_int(row.get("global_seq_num"))
    kind = _as_int(row.get("event_type"))
    ticks = _as_int(row.get("mach_time"))
    process = row.get("process")
    if seq is None or kind is None or ticks is None or not isinstance(process, dict):
        return None
    who = _token(process.get("audit_token"))
    if who is None:
        return None
    ns = _ns(ticks, numer, denom)
    body = row.get("event")
    if kind == _EVENT_EXEC:
        exec_body = body.get("exec") if isinstance(body, dict) else None
        target = exec_body.get("target") if isinstance(exec_body, dict) else None
        if not isinstance(target, dict):
            return None
        other = _token(target.get("audit_token"))
        executable = target.get("executable")
        path = executable.get("path") if isinstance(executable, dict) else None
        if other is None or not isinstance(path, str):
            return None
        return _Event(seq, kind, ns, who, other, path)
    if kind == _EVENT_FORK:
        fork_body = body.get("fork") if isinstance(body, dict) else None
        child = fork_body.get("child") if isinstance(fork_body, dict) else None
        other = _token(child.get("audit_token")) if isinstance(child, dict) else None
        if other is None:
            return None
        return _Event(seq, kind, ns, who, other, "")
    return _Event(seq, kind, ns, who, None, "")


def _parse_observer(text: str, numer: int, denom: int) -> tuple[_Event, ...] | None:
    """The observer's events in sequence order, or None when the stream is not whole."""
    events: list[_Event] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        body = _from_first_brace(raw)
        if body is None:
            return None
        try:
            row = json.loads(body)
        except ValueError:
            return None
        if not isinstance(row, dict):
            return None
        event = _parse_event(row, numer, denom)
        if event is None:
            return None
        events.append(event)
    if not events:
        return None
    events.sort(key=lambda event: event.seq)
    for before, after in pairwise(events):
        if after.seq - before.seq != 1:
            return None
    return tuple(events)


def _parse_recorder(text: str) -> tuple[tuple[_Line, ...], bool]:
    """The recorder's usable lines and whether every line declared its clock."""
    lines: list[_Line] = []
    clock_declared = True
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            clock_declared = False
            continue
        if not isinstance(row, dict):
            clock_declared = False
            continue
        numer = _as_int(row.get("tb_numer"))
        denom = _as_int(row.get("tb_denom"))
        if numer is None or denom is None or numer <= 0 or denom <= 0:
            clock_declared = False
            continue
        who = _token(row)
        ticks = _as_int(row.get("mach_abs"))
        kind = row.get("t")
        if who is None or ticks is None or not isinstance(kind, str):
            continue
        call = row.get("call")
        image = row.get("image")
        detail = row.get("detail")
        lines.append(
            _Line(
                kind=kind,
                call=call if isinstance(call, str) else "",
                who=who,
                image=_normalise_path(image) if isinstance(image, str) else "",
                ns=_ns(ticks, numer, denom),
                detail=detail if isinstance(detail, str) else "",
            )
        )
    return tuple(lines), clock_declared


def _next_end(events: tuple[_Event, ...], exec_event: _Event) -> int | None:
    """When the executed identity next execs or exits, if it does."""
    assert exec_event.other is not None
    for event in events:
        if event.seq <= exec_event.seq or event.who != exec_event.other:
            continue
        if event.kind in (_EVENT_EXEC, _EVENT_EXIT):
            return event.ns
    return None


def _walk_membership(
    tree: ModelTreeInput, events: tuple[_Event, ...]
) -> tuple[list[_Event], list[_Event]] | None:
    """The tree's exec and fork events, or None when the observer never saw its root."""
    root: Ident = (tree.root_pid, tree.root_pidversion)
    active: set[Ident] = set()
    started = False
    execs: list[_Event] = []
    forks: list[_Event] = []
    for event in events:
        if event.kind == _EVENT_EXEC and event.other is not None:
            if event.other == root and not started:
                started = True
                active.add(root)
                execs.append(event)
            elif (
                started
                and event.who in active
                and event.other[0] == event.who[0]
                and event.other != event.who
            ):
                active.discard(event.who)
                active.add(event.other)
                execs.append(event)
        elif event.kind == _EVENT_FORK and event.other is not None:
            if started and event.who in active:
                active.add(event.other)
                forks.append(event)
        elif event.kind == _EVENT_EXIT and event.who in active:
            active.discard(event.who)
    return (execs, forks) if started else None


def _read_tree(
    tree: ModelTreeInput, events: tuple[_Event, ...], lines: tuple[_Line, ...]
) -> _TreeRead:
    read = _TreeRead()
    walked = _walk_membership(tree, events)
    if walked is None:
        read.reasons.add(REASON_UNOBSERVED)
        return read
    execs, forks = walked
    read.exec_total = len(execs)
    read.fork_events = len(forks)

    loads = [(index, line) for index, line in enumerate(lines) if line.kind == "load"]
    used: set[int] = set()
    for event in execs:
        assert event.other is not None
        end = _next_end(events, event)
        for index, line in loads:
            if (
                index not in used
                and line.who == event.other
                and line.image == _normalise_path(event.path)
                and line.ns > event.ns
                and (end is None or line.ns < end)
            ):
                used.add(index)
                read.exec_met += 1
                break
        else:
            read.reasons.add(REASON_BLIND_IMAGE)

    exec_callers = {event.who for event in execs}
    child_lines = {line.who for line in lines if line.kind == "child"}
    seen_in_recorder = {line.who for line in lines}
    spawns = [
        (index, line)
        for index, line in enumerate(lines)
        if line.kind == "call" and line.call in _SPAWN_CALLS
    ]
    spawn_used: set[int] = set()
    for fork in forks:
        assert fork.other is not None
        child = fork.other
        fork_called = any(
            line.kind == "call" and line.call in _FORK_CALLS and line.who == fork.who
            for line in lines
        )
        if fork_called and child in child_lines:
            read.reconciled_children.add(child)
        else:
            spawn = next(
                (
                    index
                    for index, line in spawns
                    if index not in spawn_used
                    and line.who == fork.who
                    and line.ns <= fork.ns
                ),
                None,
            )
            if spawn is not None and child in exec_callers:
                spawn_used.add(spawn)
            else:
                read.reasons.add(REASON_MISSED_FORK)
        if child not in seen_in_recorder and child not in exec_callers:
            read.reasons.add(REASON_BLIND_CHILD)
    return read


def _is_lookup(call: str) -> bool:
    return call in _LOOKUP_CALLS or call.startswith(_LOOKUP_PREFIX)


def _graded_controls(
    request: ModelLifecycleControlsRequest,
    graded_lines: tuple[_Line, ...],
    reconciled: set[Ident],
) -> tuple[ModelControlOutcome, ...]:
    settings = request.settings
    blocked = settings.blocked_destination
    child_lines = {line.who for line in graded_lines if line.kind == "child"}
    forked_child = any(
        line.kind == "call"
        and line.call == settings.forked_child_call
        and blocked in line.detail
        and line.who in child_lines
        and line.who in reconciled
        for line in graded_lines
    )
    rejected = request.capture.packets_to_blocked_destination == 0 and any(
        line.kind == "call" and line.call == "connect" and blocked in line.detail
        for line in graded_lines
    )
    lookups = sum(
        1
        for line in graded_lines
        if line.kind == "call"
        and _is_lookup(line.call)
        and line.detail == settings.lookup_name
    )
    cached = (
        lookups >= _MIN_CACHED_LOOKUPS
        and request.capture.dns_queries_from_graded_tree == 0
    )
    return (
        ModelControlOutcome(name=CONTROL_FORKED_CHILD, passed=forked_child),
        ModelControlOutcome(name=CONTROL_REJECTED_CONNECT, passed=rejected),
        ModelControlOutcome(name=CONTROL_CACHED_LOOKUP, passed=cached),
    )


def _verdict(reasons: set[str]) -> EnumReadingVerdict:
    return EnumReadingVerdict.NOT_WORKING if reasons else EnumReadingVerdict.WORKING


def _unobserved(
    request: ModelLifecycleControlsRequest, observer_events: int
) -> ModelLifecycleControlsResult:
    results: list[ModelTreeResult] = []
    for tree in request.trees:
        names = (
            (CONTROL_FORKED_CHILD, CONTROL_REJECTED_CONNECT, CONTROL_CACHED_LOOKUP)
            if tree.role == "graded"
            else (CONTROL_BLIND_IMAGE,)
        )
        results.append(
            ModelTreeResult(
                name=tree.name,
                role=tree.role,
                verdict=EnumReadingVerdict.NOT_WORKING,
                reasons=(REASON_UNOBSERVED,),
                exec_obligations_total=0,
                exec_obligations_met=0,
                fork_events=0,
                controls=tuple(
                    ModelControlOutcome(name=name, passed=False) for name in names
                ),
            )
        )
    return ModelLifecycleControlsResult(
        lifecycle_controls="failed",
        failed_reasons=(REASON_UNOBSERVED,),
        observer_events=observer_events,
        trees=tuple(results),
    )


class HandlerLifecycleControls:
    """Pure compute: the lifecycle_controls verdict of the recorder against the observer."""

    def handle(
        self, request: ModelLifecycleControlsRequest
    ) -> ModelLifecycleControlsResult:
        events = _parse_observer(
            request.observer_jsonl,
            request.observer_timebase_numer,
            request.observer_timebase_denom,
        )
        if events is None:
            return _unobserved(request, 0)

        reads: dict[str, _TreeRead] = {}
        parsed: dict[str, tuple[_Line, ...]] = {}
        for tree in request.trees:
            lines, clock_declared = _parse_recorder(tree.recorder_jsonl)
            parsed[tree.name] = lines
            read = _read_tree(tree, events, lines)
            if not clock_declared:
                read.reasons.add(REASON_CLOCK_UNDECLARED)
            reads[tree.name] = read

        graded = [tree for tree in request.trees if tree.role == "graded"]
        graded_lines = tuple(line for tree in graded for line in parsed[tree.name])
        reconciled: set[Ident] = set()
        for tree in graded:
            reconciled |= reads[tree.name].reconciled_children
        graded_controls = _graded_controls(request, graded_lines, reconciled)

        failed: set[str] = set()
        if not all(control.passed for control in graded_controls):
            failed.add(REASON_CONTROL_FAILED)
        results: list[ModelTreeResult] = []
        for tree in request.trees:
            read = reads[tree.name]
            if tree.role == "graded":
                failed |= read.reasons
                controls = graded_controls
            else:
                # A control tree's verdict is the control's result, never the leg's.
                passed = (
                    REASON_BLIND_IMAGE in read.reasons
                    and REASON_CLOCK_UNDECLARED not in read.reasons
                )
                if REASON_CLOCK_UNDECLARED in read.reasons:
                    failed.add(REASON_CLOCK_UNDECLARED)
                if not passed:
                    failed.add(REASON_CONTROL_FAILED)
                controls = (
                    ModelControlOutcome(name=CONTROL_BLIND_IMAGE, passed=passed),
                )
            results.append(
                ModelTreeResult(
                    name=tree.name,
                    role=tree.role,
                    verdict=_verdict(read.reasons),
                    reasons=tuple(sorted(read.reasons)),
                    exec_obligations_total=read.exec_total,
                    exec_obligations_met=read.exec_met,
                    fork_events=read.fork_events,
                    controls=controls,
                )
            )
        return ModelLifecycleControlsResult(
            lifecycle_controls="failed" if failed else "passed",
            failed_reasons=tuple(sorted(failed)),
            observer_events=len(events),
            trees=tuple(results),
        )
