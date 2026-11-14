# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Serve tick tests of the stability lane refresh (OMN-21613): one pass over in-memory reader and publisher.

The reader and publisher are fakes, so no broker is needed. The golden pass publishes the pin-accepted and
completed events under one correlation id and writes the unit heartbeat; each error and refusal pass ends
on its typed topic; a pass without records exits 0 with status no_records; an overlay without a serve
block, an overlay that cannot be read and a reader that raises each end the main their own way; --probe
exits 0 and builds no reader. The last block shows the golden assertion turns red on a named break.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_stability_lane_refresh_effect import (
    runtime_stability_refresh_serve as serve,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.adapters import (
    RecordingExecutor,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.handlers.handler_stability_lane_refresh import (
    load_topics,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.models import (
    ModelRunOutcome,
    ModelStabilityServeEvidence,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.overlay import (
    ModelStabilityHostOverlay,
    ModelStabilityServeOverlay,
)
from tests.nodes.node_stability_lane_refresh_effect.stability_support import (
    PIN,
    Rig,
    overlay,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
CORRELATION = "serve-21613"
HEARTBEAT = "last-run.json"


def _payload(**changes: object) -> str:
    body: dict[str, object] = {"correlation_id": CORRELATION, "execute": True}
    body.update(changes)
    return json.dumps(body)


def _serve_overlay() -> ModelStabilityHostOverlay:
    return overlay(
        serve=ModelStabilityServeOverlay(
            bus_lane="example-bus-lane",
            workspace_root="/example/omninode",
            consumer_group="example-box-stability-refresh",
            max_records=100,
            read_timeout_s=30.0,
            join_timeout_s=10.0,
        )
    )


class FakeReader:
    def __init__(
        self, records: list[str] | None = None, *, raise_on_read: bool = False
    ) -> None:
        self.records = records if records is not None else [_payload()]
        self.raise_on_read = raise_on_read
        self.calls = 0

    def read(self, topic: str) -> list[str]:
        self.calls += 1
        if self.raise_on_read:
            raise OSError("bus unreadable")
        return self.records


class FakePublisher:
    def __init__(self, *, drop_terminal: bool = False) -> None:
        self.events: list[tuple[str, bytes, str | None]] = []
        self.drop_terminal = drop_terminal

    def publish(self, topic: str, body_bytes: bytes, key: str | None = None) -> None:
        if self.drop_terminal and topic in {
            TOPICS["stability_refresh_completed"],
            TOPICS["stability_refresh_failed"],
            TOPICS["stability_refresh_refused"],
        }:
            return
        self.events.append((topic, body_bytes, key))


def _patch_main(
    monkeypatch: pytest.MonkeyPatch,
    rig: Rig,
    reader: FakeReader,
    publisher: FakePublisher,
    *,
    host: ModelStabilityHostOverlay | None = None,
) -> None:
    """Point the main's collaborators at the fakes, so a test needs no broker, overlay file or host."""
    monkeypatch.setattr(serve, "load_overlay", lambda: host or _serve_overlay())
    monkeypatch.setattr(serve, "build_handler", lambda: rig.handler)
    monkeypatch.setattr(serve, "BusRequestReader", lambda _serve: reader)
    monkeypatch.setattr(serve, "BusEventPublisher", lambda _serve: publisher)


def _assert_golden_serve(
    evidence: ModelStabilityServeEvidence, publisher: FakePublisher
) -> None:
    assert [topic for topic, _, _ in publisher.events] == [
        TOPICS["stability_refresh_pin_accepted"],
        TOPICS["stability_refresh_completed"],
    ]
    assert {key for _, _, key in publisher.events} == {CORRELATION}
    assert json.loads(publisher.events[0][1])["tags"] == dict(PIN.as_pairs())
    assert evidence.exit_code == 0
    assert evidence.records == 1
    assert evidence.answered == 1
    assert evidence.completed == 1
    assert evidence.correlation_ids == (CORRELATION,)


def test_golden_serve_publishes_the_pin_accepted_and_completed_events() -> None:
    rig = Rig()
    reader = FakeReader()
    publisher = FakePublisher()
    evidence = serve.run_once(_serve_overlay(), rig.handler, reader, publisher)
    _assert_golden_serve(evidence, publisher)
    assert reader.calls == 1
    assert len(rig.runner.executed) == 1


def test_golden_serve_prints_one_line_and_writes_the_unit_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rig = Rig()
    publisher = FakePublisher()
    _patch_main(monkeypatch, rig, FakeReader(), publisher)
    state = tmp_path / "state"
    assert serve.main(["--state-dir", str(state)]) == 0
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    line = json.loads(out)
    assert line["status"] == "served"
    assert line["records"] == 1
    assert [topic for topic, _, _ in publisher.events] == [
        TOPICS["stability_refresh_pin_accepted"],
        TOPICS["stability_refresh_completed"],
    ]
    heartbeat = json.loads((state / HEARTBEAT).read_text(encoding="utf-8"))
    assert heartbeat["exit_code"] == 0
    assert heartbeat["result"]["status"] == "served"


def test_error_serve_a_requested_dev_tag_is_a_bad_payload_event_and_runs_nothing() -> (
    None
):
    rig = Rig()
    publisher = FakePublisher()
    requested = {
        "omnibase_infra": "dev-20261030195247-1e3c83494587",
        "omnibase_core": "v2.3.4",
        "omnimarket": "v3.4.5",
    }
    evidence = serve.run_once(
        _serve_overlay(),
        rig.handler,
        FakeReader([_payload(requested=requested)]),
        publisher,
    )
    assert [topic for topic, _, _ in publisher.events] == [
        TOPICS["stability_refresh_failed"]
    ]
    assert publisher.events[0][2] is None
    assert str(json.loads(publisher.events[0][1])["failure"]).startswith("bad-payload:")
    assert evidence.unreadable == 1
    assert evidence.answered == 0
    assert rig.runner.calls == []


def test_error_serve_a_failed_refresh_ends_on_the_failed_topic_and_exits_one() -> None:
    rig = Rig(runner=RecordingExecutor(ModelRunOutcome(return_code=2, output_tail="x")))
    publisher = FakePublisher()
    evidence = serve.run_once(_serve_overlay(), rig.handler, FakeReader(), publisher)
    assert [topic for topic, _, _ in publisher.events] == [
        TOPICS["stability_refresh_pin_accepted"],
        TOPICS["stability_refresh_failed"],
    ]
    assert evidence.failed == 1
    assert evidence.exit_code == 1


def test_refused_serve_requested_set_differs_from_the_pin_without_bump() -> None:
    rig = Rig()
    requested = {
        "omnibase_infra": "v1.2.4",
        "omnibase_core": "v2.3.4",
        "omnimarket": "v3.4.6",
    }
    publisher = FakePublisher()
    evidence = serve.run_once(
        _serve_overlay(),
        rig.handler,
        FakeReader([_payload(requested=requested)]),
        publisher,
    )
    assert [topic for topic, _, _ in publisher.events] == [
        TOPICS["stability_refresh_refused"]
    ]
    assert evidence.refused == 1
    assert evidence.exit_code == 0
    assert rig.runner.calls == []


def test_no_records_serve_exits_zero_with_no_records_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    publisher = FakePublisher()
    _patch_main(monkeypatch, Rig(), FakeReader([]), publisher)
    assert serve.main([]) == 0
    line = json.loads(capsys.readouterr().out)
    assert line["status"] == "no_records"
    assert line["records"] == 0
    assert publisher.events == []


def test_an_overlay_without_a_serve_block_exits_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_main(monkeypatch, Rig(), FakeReader(), FakePublisher(), host=overlay())
    assert serve.main([]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "serve_not_declared"


def test_an_overlay_that_cannot_be_read_exits_three_overlay_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _refused() -> None:
        raise NodeOverlayError("the overlay file is not configured")

    monkeypatch.setattr(serve, "load_overlay", _refused)
    assert serve.main([]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "overlay_refused"


def test_a_reader_that_raises_exits_three_with_source_unread(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    publisher = FakePublisher()
    _patch_main(monkeypatch, Rig(), FakeReader(raise_on_read=True), publisher)
    assert serve.main([]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "source_unread"
    assert publisher.events == []


def test_probe_exits_zero_and_builds_no_reader(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _refused(*_args: object) -> None:
        raise AssertionError(
            "the probe loads no overlay and builds no handler, reader or publisher"
        )

    monkeypatch.setattr(serve, "load_overlay", _refused)
    monkeypatch.setattr(serve, "build_handler", _refused)
    monkeypatch.setattr(serve, "BusRequestReader", _refused)
    monkeypatch.setattr(serve, "BusEventPublisher", _refused)
    assert serve.main(["--probe"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "probe_ok"}


# -- the golden assertion can fail: the named break turns it red -------------------------------


def test_break_a_dropped_terminal_event_turns_the_golden_serve_red() -> None:
    rig = Rig()
    publisher = FakePublisher(drop_terminal=True)
    evidence = serve.run_once(_serve_overlay(), rig.handler, FakeReader(), publisher)
    with pytest.raises(AssertionError):
        _assert_golden_serve(evidence, publisher)


def test_golden_serve_answers_a_command_carried_in_an_event_envelope() -> None:
    """A producer that wraps the command in an event envelope (payload) is answered like a bare command."""
    rig = Rig()
    enveloped = json.dumps(
        {"event_type": "stability-refresh-requested", "payload": json.loads(_payload())}
    )
    publisher = FakePublisher()
    evidence = serve.run_once(
        _serve_overlay(), rig.handler, FakeReader([enveloped]), publisher
    )
    _assert_golden_serve(evidence, publisher)
