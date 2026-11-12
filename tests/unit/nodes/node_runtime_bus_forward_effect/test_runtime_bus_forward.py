# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run's recorded runtime-bus events carried to the hosted bus (OMN-24003): golden and error chain tests.

A runtime-local bus keeps a run's published events in its own process; the omnibase_core runtime hands the
recording to this node after the run, and the node carries it to the hosted bus its own overlay declares —
every publish through the injected publisher, and every number (the ``max_events`` cap, the flush wait) and
the whole ``allowed_topic_prefixes`` allow-list an overlay value that refuses the load when absent.

Golden: three recorded events on allowed topics are handed to the publisher in order, key, value and headers
intact, and the flush confirms them all. Error: an event outside the allow-list, a publish the client refuses,
a flush that leaves events unconfirmed, and a recording past ``max_events`` each count as failed with the
receipt saying which and why. Failing control: widening the allow-list publishes the same event the narrow
list refused, so it is the check that refuses it, not the publisher.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_runtime_bus_forward_effect.handlers import (
    HandlerRuntimeBusForward,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.models import (
    ModelForwardEvent,
    ModelRuntimeBusForwardCommand,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.overlay import (
    NODE_NAME,
    ModelRuntimeBusForwardOverlay,
    load_overlay,
)
from omnimarket.nodes.node_runtime_bus_forward_effect.protocols import (
    ProtocolRuntimeBusPublisher,
    PublisherRefusedError,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_runtime_bus_forward_effect/overlay.example.yaml"
)
REFUSED_MESSAGE = "the hosted bus refused the publish"


def _overlay(**overrides: object) -> ModelRuntimeBusForwardOverlay:
    """The example overlay's document, with the named keys replaced by the test's values."""
    document = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    return ModelRuntimeBusForwardOverlay.model_validate({**document, **overrides})


def _event(
    topic: str,
    value: str,
    *,
    key: str | None = None,
    headers: dict[str, str] | None = None,
) -> ModelForwardEvent:
    """One recorded event, encoded the way the runtime's recorder hands it over."""
    return ModelForwardEvent(
        topic=topic,
        key_b64=(
            None
            if key is None
            else base64.b64encode(key.encode("utf-8")).decode("ascii")
        ),
        value_b64=base64.b64encode(value.encode("utf-8")).decode("ascii"),
        headers=dict(headers or {}),
    )


class RecordedPublisher:
    """The recorded fake of the publisher seam: it replays what the hosted bus took.

    Implements ``ProtocolRuntimeBusPublisher``. ``published`` holds every publish in order (topic, key, value,
    headers); a topic named in ``refuse_topics`` is answered with ``PublisherRefusedError`` instead and never
    recorded; ``flush`` answers the configured undelivered count and records the wait it was given.
    """

    def __init__(
        self, *, refuse_topics: tuple[str, ...] = (), undelivered: int = 0
    ) -> None:
        self.published: list[tuple[str, bytes | None, bytes, dict[str, str]]] = []
        self.flushes: list[float] = []
        self._refuse_topics = refuse_topics
        self._undelivered = undelivered

    def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: dict[str, str]
    ) -> None:
        if topic in self._refuse_topics:
            raise PublisherRefusedError(f"{REFUSED_MESSAGE}: {topic}")
        self.published.append((topic, key, value, headers))

    def flush(self, timeout_s: float) -> int:
        self.flushes.append(timeout_s)
        return self._undelivered

    @property
    def topics(self) -> list[str]:
        """The topics handed over, in order."""
        return [publish[0] for publish in self.published]


def _handler(
    publisher: ProtocolRuntimeBusPublisher,
    overlay: ModelRuntimeBusForwardOverlay | None = None,
) -> HandlerRuntimeBusForward:
    return HandlerRuntimeBusForward(overlay=overlay or _overlay(), publisher=publisher)


def _command(*events: ModelForwardEvent) -> ModelRuntimeBusForwardCommand:
    return ModelRuntimeBusForwardCommand(run_id="run-0417", events=events)


READING = "example.evt.omnimarket.reading-taken.v1"
VERDICT = "example.evt.omnimarket.verdict-written.v1"
RETRY = "example.cmd.omnimarket.retry-asked.v1"
UNTRUSTED = "untrusted.topic.reading.v1"


class TestGoldenChain:
    def test_three_events_are_published_in_order_and_confirmed(self) -> None:
        publisher = RecordedPublisher()
        assert isinstance(publisher, ProtocolRuntimeBusPublisher)
        result = _handler(publisher).handle(
            _command(
                _event(READING, "one", key="reading-7", headers={"trace": "t-1"}),
                _event(VERDICT, "two"),
                _event(
                    RETRY,
                    "three",
                    key="lane-2",
                    headers={"trace": "t-3", "span": "s-9"},
                ),
            )
        )
        assert publisher.published == [
            (READING, b"reading-7", b"one", {"trace": "t-1"}),
            (VERDICT, None, b"two", {}),
            (RETRY, b"lane-2", b"three", {"trace": "t-3", "span": "s-9"}),
        ]
        assert publisher.flushes == [
            10.0
        ]  # the example overlay's flush_timeout_seconds
        assert (result.forwarded, result.failed) == (3, 0)
        assert result.ok
        assert result.refusals == ()
        assert result.receipt() == {"forwarded": 3, "failed": 0, "detail": None}

    def test_an_empty_recording_is_carried_and_confirms_nothing(self) -> None:
        publisher = RecordedPublisher()
        result = _handler(publisher).handle(_command())
        assert publisher.published == []
        assert (result.forwarded, result.failed) == (0, 0)
        assert result.ok


class TestErrorChain:
    def test_an_event_outside_the_allowed_prefixes_is_refused_and_never_published(
        self,
    ) -> None:
        publisher = RecordedPublisher()
        result = _handler(publisher).handle(
            _command(
                _event(READING, "one"),
                _event(UNTRUSTED, "two"),
                _event(RETRY, "three"),
            )
        )
        assert publisher.topics == [READING, RETRY]
        assert (result.forwarded, result.failed) == (2, 1)
        assert not result.ok
        [refusal] = result.refusals
        assert refusal.topic == UNTRUSTED
        assert refusal.reason == "topic outside allowed_topic_prefixes"

    def test_a_publish_the_client_refuses_counts_failed_and_the_others_land(
        self,
    ) -> None:
        publisher = RecordedPublisher(refuse_topics=(VERDICT,))
        result = _handler(publisher).handle(
            _command(
                _event(READING, "one"), _event(VERDICT, "two"), _event(RETRY, "three")
            )
        )
        assert publisher.topics == [READING, RETRY]
        assert (result.forwarded, result.failed) == (2, 1)
        [refusal] = result.refusals
        assert refusal.topic == VERDICT
        assert refusal.reason == f"{REFUSED_MESSAGE}: {VERDICT}"

    def test_a_flush_that_leaves_two_of_three_unconfirmed_counts_them_failed(
        self,
    ) -> None:
        publisher = RecordedPublisher(undelivered=2)
        result = _handler(publisher).handle(
            _command(
                _event(READING, "one"), _event(VERDICT, "two"), _event(RETRY, "three")
            )
        )
        assert len(publisher.published) == 3
        assert (result.forwarded, result.failed) == (1, 2)
        assert result.refusals == ()
        assert not result.ok
        assert result.detail == (
            "2 event(s) not confirmed delivered within flush_timeout_seconds"
        )
        assert result.receipt() == {
            "forwarded": 1,
            "failed": 2,
            "detail": "2 event(s) not confirmed delivered within flush_timeout_seconds",
        }

    def test_a_recording_past_max_events_forwards_none_of_it(self) -> None:
        publisher = RecordedPublisher()
        events = (
            _event(READING, "one"),
            _event(VERDICT, "two"),
            _event(RETRY, "three"),
        )
        result = _handler(publisher, _overlay(max_events=2)).handle(_command(*events))
        assert publisher.published == []
        assert publisher.flushes == []
        assert (result.forwarded, result.failed) == (0, len(events))
        assert result.detail == "more than max_events"
        assert [r.topic for r in result.refusals] == [e.topic for e in events]
        assert all(r.reason == "more than max_events" for r in result.refusals)


class TestFailingControl:
    def test_widening_the_allowed_prefixes_publishes_the_same_event(self) -> None:
        publisher = RecordedPublisher()
        overlay = _overlay(
            allowed_topic_prefixes=("example.evt.", "example.cmd.", "untrusted.")
        )
        result = _handler(publisher, overlay).handle(
            _command(
                _event(READING, "one"),
                _event(UNTRUSTED, "two"),
                _event(RETRY, "three"),
            )
        )
        assert publisher.topics == [READING, UNTRUSTED, RETRY]
        assert (result.forwarded, result.failed) == (3, 0)
        assert result.ok


def _write_overlay_root(root: Path, document: dict[str, object]) -> Path:
    """The loader's documented way: ``<root>/<node>/overlay.yaml`` under a root."""
    folder = root / NODE_NAME
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return root


def _example_document() -> dict[str, object]:
    return yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))


class TestOverlay:
    def test_the_example_overlay_loads_from_the_overlay_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        roots = _write_overlay_root(tmp_path, _example_document())
        monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(roots))
        overlay = load_overlay()
        assert overlay.max_events == 500
        assert overlay.flush_timeout_seconds == 10.0
        assert overlay.allowed_topic_prefixes == ("example.evt.", "example.cmd.")
        assert overlay.event_bus.lane == "example-lane"

    def test_an_overlay_without_max_events_refuses_the_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        document = _example_document()
        del document["max_events"]
        monkeypatch.setenv(
            "ONEX_SKILL_OVERLAY_ROOTS",
            str(_write_overlay_root(tmp_path, document)),
        )
        with pytest.raises(NodeOverlayError) as raised:
            load_overlay()
        assert "max_events" in str(raised.value)

    def test_an_overlay_with_no_allowed_prefixes_refuses_the_load(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        document = _example_document()
        document["allowed_topic_prefixes"] = []
        monkeypatch.setenv(
            "ONEX_SKILL_OVERLAY_ROOTS",
            str(_write_overlay_root(tmp_path, document)),
        )
        with pytest.raises(NodeOverlayError) as raised:
            load_overlay()
        assert "allowed_topic_prefixes" in str(raised.value)


class TestSeamEntryPoint:
    """The omnibase_core side resolves its forwarder by this entry-point group; the join against core's own settle step
    is held by the seam test that waits for the core pin (OMN-24000)."""

    def test_this_node_is_the_one_forwarder_registered_for_the_runtime(self) -> None:
        from importlib.metadata import entry_points

        providers = {
            ep.name: ep.value
            for ep in entry_points(group="onex.runtime_bus_forwarders")
        }
        assert providers == {
            NODE_NAME: "omnimarket.nodes.node_runtime_bus_forward_effect.adapter_core_forwarder:CoreRuntimeBusForwarder"
        }

    def test_the_registered_forwarder_loads_and_answers_the_receipt_shape(self) -> None:
        from importlib.metadata import entry_points

        from omnimarket.nodes.node_runtime_bus_forward_effect.adapter_core_forwarder import (
            CoreRuntimeBusForwarder,
        )

        (provider,) = entry_points(group="onex.runtime_bus_forwarders")
        assert provider.load() is CoreRuntimeBusForwarder
        forwarder = CoreRuntimeBusForwarder(
            overlay=_overlay(),
            publisher_for_broker=lambda _broker: RecordedPublisher(),
        )
        assert callable(forwarder.forward)


class TestAdapter:
    """The core-facing adapter: recorded events in, the receipt mapping core validates out; a failure raises RuntimeError."""

    @staticmethod
    def _recorded(topic: str, value: str) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            topic=topic,
            key_b64=None,
            value_b64=base64.b64encode(value.encode()).decode(),
            headers={"correlation_id": "c-1"},
        )

    def test_golden_the_adapter_publishes_through_the_lane_publisher_and_answers_the_receipt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_runtime_bus_forward_effect import (
            adapter_core_forwarder as adapter,
        )

        publisher = RecordedPublisher()
        monkeypatch.setattr(adapter, "broker_for_lane", lambda lane, _env: lane)
        forwarder = adapter.CoreRuntimeBusForwarder(
            overlay=_overlay(), publisher_for_broker=lambda _lane: publisher
        )

        receipt = forwarder.forward(
            [self._recorded(READING, "one"), self._recorded(VERDICT, "two")]
        )

        assert receipt == {"forwarded": 2, "failed": 0, "detail": None}
        assert publisher.topics == [READING, VERDICT]

    def test_error_a_lane_that_cannot_be_resolved_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_runtime_bus_forward_effect import (
            adapter_core_forwarder as adapter,
        )

        def refuse(_lane: str, _env: object) -> None:
            raise OSError("lane declaration unreadable")

        monkeypatch.setattr(adapter, "broker_for_lane", refuse)
        forwarder = adapter.CoreRuntimeBusForwarder(
            overlay=_overlay(), publisher_for_broker=lambda _b: RecordedPublisher()
        )

        with pytest.raises(RuntimeError, match="lane declaration unreadable"):
            forwarder.forward([self._recorded(READING, "one")])

    def test_error_an_event_outside_the_allowed_prefixes_comes_back_in_the_receipt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnimarket.nodes.node_runtime_bus_forward_effect import (
            adapter_core_forwarder as adapter,
        )

        publisher = RecordedPublisher()
        monkeypatch.setattr(adapter, "broker_for_lane", lambda lane, _env: lane)
        forwarder = adapter.CoreRuntimeBusForwarder(
            overlay=_overlay(), publisher_for_broker=lambda _b: publisher
        )

        receipt = forwarder.forward(
            [self._recorded(READING, "one"), self._recorded(UNTRUSTED, "two")]
        )

        assert receipt["forwarded"] == 1
        assert receipt["failed"] == 1
        assert "refused" in str(receipt["detail"])
        assert publisher.topics == [READING]
