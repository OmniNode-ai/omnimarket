# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18905: the reader half of a coordinated additive injection.

Every in-process projection writer builds its ``MessageMeta`` from
``_partition``/``_offset`` in the dict the runtime hands ``handle()``, and
stamps the snapshot delta it publishes with those coordinates.
``SnapshotCache`` drops a delta whose ``source_offset`` is not greater than
the cached one from the same source topic and partition, so those two keys
decide whether a second delta for the same key is applied or discarded.

The producer does not inject them yet. That is the point of landing this
first: a key the stripper covers early is inert, while one it covers late is
a silent, offset-committing drop, so consumer-first is the only safe order
(the reasoning is spelled at ``PENDING_UPSTREAM_INJECTED_KEYS``).

These tests pin the READER contract so the producer change lands into
something already proven, and so a later edit cannot quietly drop the keys on
the floor while the injection keeps working.

Measured on the `.201` dev lane 2026-09-20: with neither key injected, a
trace subscribed to `onex.snapshot.projection.runner-fleet.v1` alone replayed
to the end of the topic (position 16,698) and still served rows stamped
`06:06:13Z`, because every delta -- snapshot offsets 0-2 and 16764-16766
alike -- carried `source_offset 0`. First writer wins forever.
"""

from __future__ import annotations

import ast
import importlib
import json
import pickle
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.handler_projection_runtime_writer import (
    RuntimeProjectionWriter,
)
from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.models.model_projection_delivery_context_error import (
    ProjectionDeliveryContextMissingError,
)
from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_savings.handlers.handler_runtime_writer import (
    SavingsProjectionWriter,
)
from omnimarket.projection.handler_shim import (
    INJECTED_OFFSET_KEY,
    INJECTED_PARTITION_KEY,
    PENDING_UPSTREAM_INJECTED_KEYS,
    RUNTIME_INJECTED_KEYS,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit

#: The four classes the OMN-16874 roster declares for in-process dispatch.
#: Walked from the contracts rather than hand-listed, so a fifth writer is
#: covered the day it lands.
_ATTR = "onex_runtime_inprocess_dispatch"


def _inprocess_writers() -> list[type[Any]]:
    import pathlib

    nodes = pathlib.Path(__file__).parent.parent / "src/omnimarket/nodes"
    found: list[type[Any]] = []
    for node_dir in sorted(nodes.glob("node_projection_*")):
        contract = node_dir / "contract.yaml"
        if not contract.exists():
            continue
        routing = yaml.safe_load(contract.read_text()).get("handler_routing") or {}
        for entry in routing.get("handlers", []):
            ref = entry.get("handler") or {}
            name, module = ref.get("name"), ref.get("module")
            if not name or not module:
                continue
            klass = getattr(importlib.import_module(module), name, None)
            if klass is not None and getattr(klass, _ATTR, False):
                found.append(klass)
    return found


def test_the_walk_finds_the_writers_at_all() -> None:
    """Guard against an inert suite: an empty walk would pass everything."""
    writers = _inprocess_writers()
    assert len(writers) >= 4, [w.__name__ for w in writers]


def test_every_inprocess_writer_reads_both_source_coordinates() -> None:
    """The reader contract, asserted on structure rather than by running Kafka.

    A projection runner has no ``handle`` of its own: the one synchronous entry
    is ``BaseProjectionRunner.handle``, which splits the injected dict through
    ``ModelProjectionRequest.from_injected``. So the guard is that no
    runner overrides that entry (a runner that did could stop reading the keys
    and publish fixed coordinates again, silently re-entering the
    first-writer-wins failure), and that the split itself carries both keys.
    A writer that is not a projection runner keeps its own ``handle``, and its
    source must still read both keys.
    """
    import inspect

    for writer in _inprocess_writers():
        if issubclass(writer, BaseProjectionRunner):
            assert "handle" not in vars(writer), (
                f"{writer.__name__} overrides handle; the coordinates are read "
                "once, in BaseProjectionRunner.handle, and an override can drop them"
            )
            assert writer.handle is BaseProjectionRunner.handle
            continue
        source = inspect.getsource(writer.handle)
        if "require_delivery_context" in source:
            # The typed read: the split carries both keys (the next test) and
            # ``require_delivery_context`` refuses a request that lacks either,
            # so the writer cannot publish a defaulted coordinate.
            assert "ModelProjectionRequest.from_injected" in source, (
                f"{writer.__name__}.handle requires a delivery context it did "
                "not read through the shared split"
            )
            continue
        assert INJECTED_PARTITION_KEY in source, (
            f"{writer.__name__}.handle does not read {INJECTED_PARTITION_KEY!r}; "
            "its deltas would carry a fixed partition"
        )
        assert INJECTED_OFFSET_KEY in source, (
            f"{writer.__name__}.handle does not read {INJECTED_OFFSET_KEY!r}; "
            "its deltas would carry a fixed offset and every one after the "
            "first for a given key would be dropped as a replay"
        )


def test_the_shared_split_reads_both_source_coordinates() -> None:
    """The single reader every projection runner shares carries both keys."""
    request = ModelProjectionRequest.from_injected(
        {
            "_topic": "t",
            INJECTED_PARTITION_KEY: 3,
            INJECTED_OFFSET_KEY: 4242,
            "field": "kept",
        }
    )
    assert (request.partition, request.offset) == (3, 4242)
    assert request.has_coordinates
    assert request.payload == {"field": "kept"}


def test_a_writer_stamps_the_injected_coordinates_onto_its_meta() -> None:
    """Behavioural, not textual: the keys reach MessageMeta.

    The source assertion above catches a deletion; this catches the subtler
    case where the keys are read and then not threaded through.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    seen: dict[str, Any] = {}

    class _Recording(LabLaneHealthProjectionWriter):
        @property
        def db(self) -> Any:
            class _Db:
                async def connect(self) -> None: ...

                async def close(self) -> None: ...

            return _Db()

        async def _project_and_report(  # type: ignore[override]
            self, topic: str, data: dict[str, Any], meta: Any
        ) -> list[Any]:
            seen["partition"] = meta.partition
            seen["offset"] = meta.offset
            return []

    _Recording().handle(
        {
            "lane": "compose-dev",
            "timestamp": "2026-09-20T10:05:45+00:00",
            "_topic": "onex.evt.omnibase-infra.runtime-health-check.v1",
            INJECTED_PARTITION_KEY: 3,
            INJECTED_OFFSET_KEY: 4242,
        }
    )

    assert seen == {"partition": 3, "offset": 4242}


def test_absent_coordinates_keep_todays_behaviour() -> None:
    """Explicitly pinned, because the producer does not inject them yet.

    Until the injection lands, every dispatch arrives without these keys and
    must keep working exactly as it does now. This is what makes the reader
    change inert rather than a flag day.
    """
    from omnimarket.nodes.node_projection_lab_lane_health.handlers.handler_lab_lane_health_runner import (
        LabLaneHealthProjectionWriter,
    )

    seen: dict[str, Any] = {}

    class _Recording(LabLaneHealthProjectionWriter):
        @property
        def db(self) -> Any:
            class _Db:
                async def connect(self) -> None: ...

                async def close(self) -> None: ...

            return _Db()

        async def _project_and_report(  # type: ignore[override]
            self, topic: str, data: dict[str, Any], meta: Any
        ) -> list[Any]:
            seen["partition"] = meta.partition
            seen["offset"] = meta.offset
            seen["payload_keys"] = sorted(data)
            return []

    _Recording().handle(
        {
            "lane": "compose-dev",
            "timestamp": "2026-09-20T10:05:45+00:00",
            "_topic": "onex.evt.omnibase-infra.runtime-health-check.v1",
        }
    )

    assert seen["partition"] == 0
    assert seen["offset"] == 0
    # And the transport keys never reach the fold in either direction.
    assert seen["payload_keys"] == ["lane", "timestamp"]


def test_both_keys_are_declared_consumer_first() -> None:
    """The stripper covers them now; the producer catches up later.

    The seam guard requires a pending key to also be in the stripper set, so
    these two assertions together are what make the additive pair landable in
    the safe order.
    """
    assert INJECTED_PARTITION_KEY in RUNTIME_INJECTED_KEYS
    assert INJECTED_OFFSET_KEY in RUNTIME_INJECTED_KEYS
    assert INJECTED_PARTITION_KEY in PENDING_UPSTREAM_INJECTED_KEYS
    assert INJECTED_OFFSET_KEY in PENDING_UPSTREAM_INJECTED_KEYS
    # The pending entries are deleted when the omnibase_infra floor rises to
    # the release carrying the injection; the seam guard turns a stale entry
    # red rather than leaving it as a permanent exemption.
    assert PENDING_UPSTREAM_INJECTED_KEYS <= RUNTIME_INJECTED_KEYS


# A delivery with no context (a runtime tick, a replay, a direct dispatch) arrives
# without the two coordinates. Both writers refuse it with a typed error that names
# the missing keys; the tests below carry ``partition_missing_delivery_context`` in
# their names.

SAVINGS_TOPIC = "onex.evt.omnibase-infra.savings-estimated.v1"
HOOK_TOPIC = "onex.evt.omniclaude.hook-event.v1"
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "projection_writer_partition_missing_delivery_context_incident.json"
)
SAVINGS_CONTRACT = (
    Path(__file__).parents[1]
    / "src/omnimarket/nodes/node_projection_savings/contract.yaml"
)


class _RecordingRunner:
    """Stands in for the runner: records what reached it, opens nothing."""

    constructed: ClassVar[int] = 0
    seen: ClassVar[list[tuple[str, dict[str, Any], MessageMeta]]] = []

    def __init__(self) -> None:
        type(self).constructed += 1

    async def project_runtime_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        type(self).seen.append((topic, data, meta))
        return True

    async def close_runtime_resources(self) -> None:
        return None


class _BaseWriter(RuntimeProjectionWriter):
    """The shared base writer, with the recording runner bound by ``base``."""


@pytest.fixture(autouse=True)
def _reset_runner() -> None:
    _RecordingRunner.constructed = 0
    _RecordingRunner.seen = []


@pytest.fixture
def base(monkeypatch: pytest.MonkeyPatch) -> _BaseWriter:
    monkeypatch.setattr(_BaseWriter, "runner_type", _RecordingRunner, raising=False)
    return _BaseWriter()


@pytest.fixture
def savings(monkeypatch: pytest.MonkeyPatch) -> SavingsProjectionWriter:
    monkeypatch.setattr(SavingsProjectionWriter, "runner_type", _RecordingRunner)
    return SavingsProjectionWriter()


def _delivery_less(topic: str, **payload: Any) -> dict[str, Any]:
    """The dict the runtime builds when it has no delivery: no coordinates at all."""
    return {
        "_db": object(),
        "_event_type": "evt",
        "_topic": topic,
        "_envelope_id": "00000000-0000-4000-8000-000000000009",
        **payload,
    }


@pytest.mark.parametrize(
    ("present", "missing"),
    [
        ({}, ("_partition", "_offset")),
        ({"_offset": 7}, ("_partition",)),
        ({"_partition": 3}, ("_offset",)),
    ],
    ids=["neither", "no-partition", "no-offset"],
)
def test_partition_missing_delivery_context_base_writer_names_the_keys(
    base: _BaseWriter, present: dict[str, int], missing: tuple[str, ...]
) -> None:
    with pytest.raises(ProjectionDeliveryContextMissingError) as raised:
        base.handle(_delivery_less(SAVINGS_TOPIC, **present))
    assert raised.value.missing_keys == missing
    assert raised.value.topic == SAVINGS_TOPIC
    for key in missing:
        assert key in str(raised.value)
    assert _RecordingRunner.seen == []


@pytest.mark.parametrize("topic", [SAVINGS_TOPIC, HOOK_TOPIC])
@pytest.mark.parametrize(
    ("present", "missing"),
    [
        ({}, ("_partition", "_offset")),
        ({"_offset": 7}, ("_partition",)),
        ({"_partition": 3}, ("_offset",)),
    ],
    ids=["neither", "no-partition", "no-offset"],
)
def test_partition_missing_delivery_context_savings_writer_names_the_keys(
    savings: SavingsProjectionWriter,
    topic: str,
    present: dict[str, int],
    missing: tuple[str, ...],
) -> None:
    with pytest.raises(ProjectionDeliveryContextMissingError) as raised:
        savings.handle(_delivery_less(topic, hook_event_name="PreToolUse", **present))
    assert raised.value.missing_keys == missing
    assert raised.value.topic == topic
    assert _RecordingRunner.constructed == 0


def test_partition_missing_delivery_context_names_a_missing_topic_too(
    base: _BaseWriter,
) -> None:
    request = _delivery_less(SAVINGS_TOPIC)
    del request["_topic"]
    with pytest.raises(ProjectionDeliveryContextMissingError) as raised:
        base.handle({**request, "_partition": 0, "_offset": 0})
    assert raised.value.missing_keys == ("_topic",)
    assert raised.value.topic is None


def test_partition_missing_delivery_context_error_is_not_a_content_or_wiring_type(
    base: _BaseWriter,
) -> None:
    """The consume boundary withholds on it: not a validation, key or type error."""
    with pytest.raises(ProjectionDeliveryContextMissingError) as raised:
        base.handle(_delivery_less(SAVINGS_TOPIC))
    assert not isinstance(
        raised.value, KeyError | TypeError | ValueError | ValidationError
    )


def test_partition_missing_delivery_context_message_is_stable_across_records(
    base: _BaseWriter,
) -> None:
    """One record refused twice renders one message, so the withhold bound counts it."""
    messages = set()
    for event_id in ("a", "b"):
        with pytest.raises(ProjectionDeliveryContextMissingError) as raised:
            base.handle(_delivery_less(SAVINGS_TOPIC, event_id=event_id))
        messages.add(str(raised.value))
    assert len(messages) == 1


def test_partition_missing_delivery_context_error_survives_pickling() -> None:
    error = ProjectionDeliveryContextMissingError(("_partition",), SAVINGS_TOPIC)
    clone = pickle.loads(pickle.dumps(error))
    assert (clone.missing_keys, clone.topic, str(clone)) == (
        error.missing_keys,
        error.topic,
        str(error),
    )


@pytest.mark.parametrize("coordinate", [0, 1])
def test_partition_missing_delivery_context_zero_is_a_coordinate_not_an_absence(
    base: _BaseWriter, coordinate: int
) -> None:
    out = base.handle(
        _delivery_less(SAVINGS_TOPIC, _partition=coordinate, _offset=coordinate)
    )
    assert out == {"projected": True}
    topic, data, meta = _RecordingRunner.seen[0]
    assert topic == SAVINGS_TOPIC
    assert meta.partition == coordinate
    assert meta.offset == coordinate
    for key in ("_db", "_topic", "_partition", "_offset", "_fallback_id"):
        assert key not in data
    assert data["_event_type"] == "evt"


def test_partition_missing_delivery_context_supplied_fallback_id_is_kept(
    base: _BaseWriter,
) -> None:
    base.handle(
        _delivery_less(SAVINGS_TOPIC, _partition=1, _offset=2, _fallback_id="fb-1")
    )
    assert _RecordingRunner.seen[0][2].fallback_id == "fb-1"


def test_partition_missing_delivery_context_savings_still_skips_ignored_hook_event(
    savings: SavingsProjectionWriter,
) -> None:
    out = savings.handle(
        _delivery_less(
            HOOK_TOPIC, _partition=0, _offset=5, hook_event_name="PreToolUse"
        )
    )
    assert out == {"projected": True}
    assert _RecordingRunner.constructed == 0


def test_partition_missing_delivery_context_request_reports_the_missing_keys() -> None:
    both = ModelProjectionRequest.from_injected({"_topic": "t"})
    assert both.missing_coordinates == ("_partition", "_offset")
    assert not both.has_coordinates
    only_offset = ModelProjectionRequest.from_injected({"_topic": "t", "_offset": 1})
    assert only_offset.missing_coordinates == ("_partition",)
    full = ModelProjectionRequest.from_injected(
        {"_topic": "t", "_partition": 0, "_offset": 0}
    )
    assert full.missing_coordinates == ()
    assert full.has_coordinates
    assert full.require_delivery_context() is full


def test_partition_missing_delivery_context_request_refuses_with_the_typed_error() -> (
    None
):
    with pytest.raises(ProjectionDeliveryContextMissingError) as raised:
        ModelProjectionRequest.from_injected(
            {"_partition": 1}
        ).require_delivery_context()
    assert raised.value.missing_keys == ("_topic", "_offset")


def _fixture() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(FIXTURE.read_text())
    return loaded


def _incident_requests() -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = _fixture()["requests"]
    return requests


def test_partition_missing_delivery_context_incident_fixture_matches_the_contract() -> (
    None
):
    """Every fixture topic is one the savings contract subscribes to."""
    contract = yaml.safe_load(SAVINGS_CONTRACT.read_text())
    subscribed = set(contract["event_bus"]["subscribe_topics"])
    assert {request["_topic"] for request in _incident_requests()} <= subscribed
    assert _fixture()["incident"]["keyerror_text"] == "KeyError: '_partition'"


@pytest.mark.parametrize("recorded", _incident_requests(), ids=lambda r: str(r["name"]))
def test_partition_missing_delivery_context_incident_dispatch_ends_typed(
    savings: SavingsProjectionWriter, recorded: dict[str, Any]
) -> None:
    """The 2026-11-21 09:10Z wedge: a delivery-less dispatch reached the savings writer."""
    request = {
        "_db": object(),
        "_event_type": recorded["_event_type"],
        "_topic": recorded["_topic"],
        "_envelope_id": recorded["_envelope_id"],
        **recorded["payload"],
    }
    assert "_partition" not in request
    assert "_offset" not in request
    with pytest.raises(ProjectionDeliveryContextMissingError) as raised:
        savings.handle(request)
    assert not isinstance(raised.value, KeyError)
    assert raised.value.missing_keys == ("_partition", "_offset")
    assert raised.value.topic == recorded["_topic"]
    assert _RecordingRunner.seen == []


# OMN-26355: no package source reads a delivery coordinate without a default.
# A subscript or one-argument pop of _partition or _offset turns a dispatch that
# carries no delivery context into a bare KeyError; a read that supplies a default
# raises none and is out of this guard's reach by design.
_COORDINATE_SCAN_ROOT = Path(__file__).resolve().parents[1] / "src" / "omnimarket"
_DELIVERY_COORDINATE_KEYS = frozenset(
    {"_partition", "_offset", "INJECTED_PARTITION_KEY", "INJECTED_OFFSET_KEY"}
)


def _names_a_coordinate(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in _DELIVERY_COORDINATE_KEYS
    return isinstance(node, ast.Name) and node.id in _DELIVERY_COORDINATE_KEYS


def _is_subscript_read(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _names_a_coordinate(node.slice)
    )


def _is_default_less_pop(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "pop"
        and len(node.args) == 1
        and not node.keywords
        and _names_a_coordinate(node.args[0])
    )


def unconditional_coordinate_reads(source: str) -> list[int]:
    """The line of every subscript or default-less ``pop`` of a delivery coordinate."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if _is_subscript_read(node) or _is_default_less_pop(node)
    )


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        ('partition = int(data.pop("_partition"))', [1]),
        ('offset = int(request["_offset"])', [1]),
        ("partition = request[INJECTED_PARTITION_KEY]", [1]),
        ('x = 1\ny = data.pop("_offset")', [2]),
        ('partition = int(data.pop("_partition", 0))', []),
        ('partition = data.pop("_partition", None)', []),
        ('partition = data.get("_partition")', []),
        ('data["_partition"] = 3', []),
        ('topic = data.pop("_topic")', []),
    ],
)
def test_scanner_flags_only_the_reads_that_raise_key_error(
    source: str, expected_lines: list[int]
) -> None:
    """Positive control: the reads that wedged dev are found, the defaulted ones are not."""
    assert unconditional_coordinate_reads(source) == expected_lines


def test_partition_missing_delivery_context_no_source_reads_a_coordinate_unconditionally() -> (
    None
):
    """Every package source reads ``_partition`` and ``_offset`` through the typed request."""
    sources = [
        path
        for path in sorted(_COORDINATE_SCAN_ROOT.rglob("*.py"))
        if "tests" not in path.relative_to(_COORDINATE_SCAN_ROOT).parts
    ]
    assert len(sources) > 100, "the scan found no package tree to read"
    offenders = {
        str(path.relative_to(_COORDINATE_SCAN_ROOT)): lines
        for path in sources
        if (lines := unconditional_coordinate_reads(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        "these sources read a runtime-injected delivery coordinate with no default; "
        "read it through ModelProjectionRequest.require_delivery_context instead: "
        f"{offenders}"
    )
