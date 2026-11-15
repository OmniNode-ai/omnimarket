# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev-tag-heartbeat condition of node_lab_alarm and its seam to the alarm delivery.

Golden: a fresh, unalarmed beat for every repository reads OK and the event chain is started then
completed; a breach that reaches the overlay's persistence bound alarms, is delivered once through the
delivery double, and the next OK ends the episode. Error: an unreadable source, a document with no
heartbeat object, or a malformed observed_at is INDETERMINATE, never ALARM and never OK; a failed
delivery exits as an alarm and names delivery-failed; the failed terminal event is published on a
breach. Failing control: the same beat with the bound raised past its age reads OK, and with the bound
as declared reads ALARM, so the bound in the overlay is what alarms; persist_runs 3 gives OK on runs 1
and 2 and ALARM on run 3, and a good read in between resets the streak. Seam: the alarm line a run
produces, split at the first ': ', builds a valid ModelLabAlarm accepted by the real
HandlerLabAlarmDeliver with the in-memory backend; every topic name the handler's events_for emits is
declared in the contract's topics and externally_consumed_topics. The overlay loads without the block,
the verb refuses an overlay without the block, the CLI exit codes follow the outcome, and the
HttpHeartbeatSource reads a local one-shot http.server answer and refuses non-200 and non-JSON bodies.
"""

from __future__ import annotations

import http.server
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import ModelLabAlarm
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryGrowthStreaks,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    HttpHeartbeatSource,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_dev_tag_heartbeat import (
    CONDITION,
    HandlerDevTagHeartbeat,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_dev_tag_heartbeat import (
    EnumDevTagHeartbeatFault,
    EnumDevTagHeartbeatOutcome,
    ModelDevTagHeartbeatCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelConditionPersistence,
    ModelDevTagHeartbeatSettings,
    ModelLabAlarmOverlay,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    HeartbeatUnreadableError,
)
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import (
    build_dev_tag_heartbeat_handler,
    run_dev_tag_heartbeat,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CLOCK = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)
_REPO = "example-repo"


class _HeartbeatSource:
    """Serves one heartbeat document, or refuses with a typed error."""

    def __init__(self, document: dict[str, object] | None = None) -> None:
        self.document = document
        self.unreadable: str | None = None

    def read(self) -> dict[str, object]:
        if self.unreadable is not None:
            raise HeartbeatUnreadableError(self.unreadable)
        assert self.document is not None
        return self.document


def _settings(
    repositories: tuple[str, ...] = (_REPO,),
    max_age_seconds: int = 300,
    persist_runs: int = 1,
) -> ModelDevTagHeartbeatSettings:
    return ModelDevTagHeartbeatSettings(
        agent_url="http://agent.example:8080",
        endpoint_path="/v1/heartbeat",
        repositories=repositories,
        max_age_seconds=max_age_seconds,
        timeout_seconds=10,
        persistence=ModelConditionPersistence(
            persist_runs=persist_runs,
            streak_path=".onex_state/lab-alarm/dev-tag-heartbeat-streaks.json",
        ),
    )


def _command(
    *,
    write: bool = True,
    fault: EnumDevTagHeartbeatFault = EnumDevTagHeartbeatFault.NONE,
) -> ModelDevTagHeartbeatCommand:
    return ModelDevTagHeartbeatCommand(
        correlation_id="heartbeat-1", write=write, fault=fault
    )


def _fresh_document() -> dict[str, object]:
    return {
        "heartbeat": {
            _REPO: {
                "observed_at": _CLOCK.isoformat(),
                "alarm": False,
                "newest_tag": "dev-42",
                "running_ref": "dev-41",
                "lag_seconds": 0,
                "alarm_after_seconds": 3600,
            }
        }
    }


def _stale_document() -> dict[str, object]:
    return {
        "heartbeat": {
            _REPO: {
                "observed_at": "2026-11-12T03:54:00+00:00",
                "alarm": False,
                "newest_tag": "dev-42",
                "running_ref": "dev-41",
                "lag_seconds": 0,
                "alarm_after_seconds": 3600,
            }
        }
    }


def _alarmed_document() -> dict[str, object]:
    return {
        "heartbeat": {
            _REPO: {
                "observed_at": _CLOCK.isoformat(),
                "alarm": True,
                "newest_tag": "dev-42",
                "running_ref": "dev-41",
                "lag_seconds": 900,
                "alarm_after_seconds": 600,
            }
        }
    }


class _Rig:
    def __init__(
        self,
        document: dict[str, object] | None = None,
        *,
        settings: ModelDevTagHeartbeatSettings | None = None,
        deliver: bool = True,
    ) -> None:
        self.source = _HeartbeatSource(
            document if document is not None else _fresh_document()
        )
        self.streaks = InMemoryGrowthStreaks()
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.handler = HandlerDevTagHeartbeat(
            settings=settings if settings is not None else _settings(),
            source=self.source,
            streaks=self.streaks,
            delivery=delivery if deliver else None,
            clock=lambda: _CLOCK,
        )


def test_dev_tag_heartbeat_golden_chain() -> None:
    """A healthy beat reads OK and the event chain is started then completed; a breach reaching
    persist_runs alarms, delivers once through the delivery double and the next OK ends the episode."""
    topics = load_topics()
    rig = _Rig()
    golden = rig.handler.handle(_command())
    assert golden.outcome is EnumDevTagHeartbeatOutcome.OK
    assert golden.alarms == ()
    assert golden.reasons == ()
    assert golden.failure == ""
    assert [t for t, _ in events_for(golden, topics)] == [
        topics["dev_tag_heartbeat_run_started"],
        topics["dev_tag_heartbeat_run_completed"],
    ]
    rig.source.document = _alarmed_document()
    breach = rig.handler.handle(_command())
    assert breach.outcome is EnumDevTagHeartbeatOutcome.ALARM
    [alarm] = breach.alarms
    assert alarm.startswith(f"dev-tag-heartbeat:{_REPO}: ")
    assert "the lane is behind example-repo's newest dev tag dev-42" in alarm
    assert "running dev-41" in alarm
    assert "behind for 900s" in alarm
    assert "past the agent's 600s bound" in alarm
    assert breach.delivery == (
        f"{CONDITION}:dev-tag-heartbeat:{_REPO} channel=delivered ledger=delivered",
    )
    [(_, text)] = rig.channel.posts
    assert _REPO in text
    [(_, row)] = rig.ledger.rows
    assert f"subject=dev-tag-heartbeat:{_REPO}" in row.split(" | ")
    rig.source.document = _fresh_document()
    ok = rig.handler.handle(_command())
    assert ok.outcome is EnumDevTagHeartbeatOutcome.OK
    assert rig.streaks.streaks == {}, "the OK run cleared the streak"
    rig.source.document = _alarmed_document()
    rig.handler.handle(_command())
    assert len(rig.channel.posts) == 2
    assert len(rig.ledger.rows) == 1, "the ledger window still holds the first row"


def test_dev_tag_heartbeat_error_chain() -> None:
    """Unreadable source, no heartbeat object, malformed observed_at are INDETERMINATE, never ALARM
    or OK; a failed delivery exits 2 and names delivery-failed; the failed terminal event is published
    on a breach."""
    topics = load_topics()
    rig = _Rig()
    rig.source.unreadable = "connection refused"
    unreadable = rig.handler.handle(_command())
    assert unreadable.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
    assert unreadable.reasons == ("heartbeat-unreadable",)
    assert unreadable.alarms == ()
    assert unreadable.failure == "indeterminate:heartbeat-unreadable"
    assert [t for t, _ in events_for(unreadable, topics)][-1] == topics[
        "dev_tag_heartbeat_run_failed"
    ]
    rig2 = _Rig(document={"other": 1})
    no_object = rig2.handler.handle(_command())
    assert no_object.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
    assert no_object.reasons == ("no-heartbeat-object",)
    rig3 = _Rig(document={"heartbeat": {_REPO: {"observed_at": 42}}})
    malformed = rig3.handler.handle(_command())
    assert malformed.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
    assert malformed.reasons == ("beat-malformed",)
    rig4 = _Rig(document={"heartbeat": {_REPO: {"observed_at": "not-a-date"}}})
    unparseable = rig4.handler.handle(_command())
    assert unparseable.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
    assert unparseable.reasons == ("beat-malformed",)
    rig5 = _Rig(document=_alarmed_document())
    rig5.ledger.refuse = "ledger host unreachable"
    failed = rig5.handler.handle(_command())
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure
    assert [t for t, _ in events_for(failed, topics)][-1] == topics[
        "dev_tag_heartbeat_run_failed"
    ]


def test_dev_tag_heartbeat_failing_control() -> None:
    """The same beat with max_age_seconds raised past its age reads OK, and with the bound as declared
    reads ALARM, so the bound in the overlay is what alarms; persist_runs 3 gives OK on runs 1 and 2 and
    ALARM on run 3 while a good read in between resets the streak."""
    stale = _stale_document()
    rig = _Rig(document=stale, settings=_settings(max_age_seconds=3600))
    assert rig.handler.handle(_command()).outcome is EnumDevTagHeartbeatOutcome.OK
    rig2 = _Rig(document=stale, settings=_settings(max_age_seconds=300))
    assert rig2.handler.handle(_command()).outcome is EnumDevTagHeartbeatOutcome.ALARM
    rig3 = _Rig(document=_alarmed_document(), settings=_settings(persist_runs=3))
    first = rig3.handler.handle(_command())
    assert first.outcome is EnumDevTagHeartbeatOutcome.OK
    assert first.reasons == (f"waiting:{_REPO}: run 1 of 3",)
    second = rig3.handler.handle(_command())
    assert second.outcome is EnumDevTagHeartbeatOutcome.OK
    assert second.reasons == (f"waiting:{_REPO}: run 2 of 3",)
    rig3.source.document = _fresh_document()
    reset = rig3.handler.handle(_command())
    assert reset.outcome is EnumDevTagHeartbeatOutcome.OK
    assert reset.reasons == ()
    assert rig3.streaks.streaks == {}, "the good read cleared the streak"
    rig3.source.document = _alarmed_document()
    rig3.handler.handle(_command())
    rig3.handler.handle(_command())
    third = rig3.handler.handle(_command())
    assert third.outcome is EnumDevTagHeartbeatOutcome.ALARM
    assert third.reasons == ()


def test_dev_tag_heartbeat_seam() -> None:
    """The alarm line a run produces, split at the first ': ', builds a valid ModelLabAlarm accepted by
    the real HandlerLabAlarmDeliver with the in-memory backend; every topic name the handler's
    events_for emits is declared in the contract's topics and externally_consumed_topics."""
    rig = _Rig(document=_alarmed_document())
    result = rig.handler.handle(_command())
    [alarm] = result.alarms
    subject, separator, detail = alarm.partition(": ")
    assert separator == ": "
    assert subject == f"dev-tag-heartbeat:{_REPO}"
    model = ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail)
    assert model.condition == CONDITION
    assert model.subject == subject
    assert model.detail == detail
    topics = load_topics()
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    declared = set(contract["topics"])
    external = set(contract.get("externally_consumed_topics", []))
    emitted = {t for t, _ in events_for(result, topics)}
    assert {k for k, v in topics.items() if v in emitted} <= declared
    assert emitted <= set(contract["topics"].values())
    assert emitted <= external, "every emitted topic is declared as externally consumed"


def _example_raw() -> dict[str, object]:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_dev_tag_heartbeat_overlay_loads_without_the_block() -> None:
    raw = _example_raw()
    assert ModelLabAlarmOverlay.model_validate(raw).dev_tag_heartbeat is not None
    del raw["dev_tag_heartbeat"]
    assert ModelLabAlarmOverlay.model_validate(raw).dev_tag_heartbeat is None


def test_dev_tag_heartbeat_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _example_raw()
    del raw["dev_tag_heartbeat"]
    code = run_dev_tag_heartbeat(
        "dev-tag-heartbeat",
        EnumDevTagHeartbeatFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
    )
    assert code == 1
    assert "no dev_tag_heartbeat block" in capsys.readouterr().err


def test_dev_tag_heartbeat_cli_exit_codes_follow_the_outcome() -> None:
    overlay = ModelLabAlarmOverlay.model_validate(_example_raw())
    rig = _Rig(document=_alarmed_document())
    lines: list[str] = []
    assert (
        run_dev_tag_heartbeat(
            "dev-tag-heartbeat-probe",
            EnumDevTagHeartbeatFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 2
    )
    assert any(line.startswith("DEV-TAG-HEARTBEAT outcome=ALARM") for line in lines)
    assert any(line.startswith(f"ALARM dev-tag-heartbeat:{_REPO}") for line in lines)
    assert rig.streaks.streaks == {}, "a probe keeps no streak"
    assert rig.channel.posts == [], "a probe delivers nothing"
    rig.source.document = _fresh_document()
    assert (
        run_dev_tag_heartbeat(
            "dev-tag-heartbeat",
            EnumDevTagHeartbeatFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 0
    )
    rig.source.unreadable = "connection refused"
    assert (
        run_dev_tag_heartbeat(
            "dev-tag-heartbeat",
            EnumDevTagHeartbeatFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 3
    )


def test_dev_tag_heartbeat_cli_exits_alarm_when_the_delivery_fails() -> None:
    rig = _Rig(document=_alarmed_document())
    rig.ledger.refuse = "ledger host unreachable"
    code = run_dev_tag_heartbeat(
        "dev-tag-heartbeat",
        EnumDevTagHeartbeatFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(_example_raw()),
        handler=rig.handler,
        out=lambda _: None,
    )
    assert code == 2


def test_dev_tag_heartbeat_a_drill_alarms_without_reading_keeping_or_delivering() -> (
    None
):
    rig = _Rig()
    result = rig.handler.handle(_command(fault=EnumDevTagHeartbeatFault.STALE_BEAT))
    assert result.outcome is EnumDevTagHeartbeatOutcome.ALARM
    assert rig.streaks.streaks == {}
    assert rig.channel.posts == []
    assert result.delivery == ()


def test_dev_tag_heartbeat_a_probe_keeps_no_streak_and_a_write_does() -> None:
    rig = _Rig(document=_alarmed_document(), settings=_settings(persist_runs=3))
    rig.handler.handle(_command(write=False))
    assert rig.streaks.streaks == {}
    rig.handler.handle(_command())
    assert rig.streaks.streaks == {_REPO: 1}
    rig.source.unreadable = "connection refused"
    rig.handler.handle(_command())
    assert rig.streaks.streaks == {_REPO: 1}, "an unreadable run leaves the streak"


def test_dev_tag_heartbeat_inmemory_backend_builds_the_fixture_source() -> None:
    overlay = ModelLabAlarmOverlay.model_validate(_example_raw())
    assert overlay.dev_tag_heartbeat is not None
    handler = build_dev_tag_heartbeat_handler(
        overlay, overlay.dev_tag_heartbeat, lambda: _CLOCK
    )
    result = handler.handle(_command(write=False))
    assert result.outcome is EnumDevTagHeartbeatOutcome.INDETERMINATE
    assert result.reasons == ("heartbeat-unreadable",)


def _serve(status: int, body: bytes) -> tuple[http.server.HTTPServer, str]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/x"


def test_http_heartbeat_source_reads_a_local_server() -> None:
    server, url = _serve(200, json.dumps(_fresh_document()).encode("utf-8"))
    try:
        document = HttpHeartbeatSource(url, 5).read()
    finally:
        server.shutdown()
    assert document["heartbeat"][_REPO]["newest_tag"] == "dev-42"


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, b"{}"), (200, b"not json"), (200, b"[1, 2]")],
    ids=["non-200", "non-json", "not-an-object"],
)
def test_http_heartbeat_source_refuses_an_answer_that_is_not_a_document(
    status: int, body: bytes
) -> None:
    server, url = _serve(status, body)
    try:
        with pytest.raises(HeartbeatUnreadableError):
            HttpHeartbeatSource(url, 5).read()
    finally:
        server.shutdown()


def test_http_heartbeat_source_refuses_a_closed_port_and_a_non_http_url() -> None:
    with pytest.raises(HeartbeatUnreadableError):
        HttpHeartbeatSource("http://127.0.0.1:9/x", 2).read()
    with pytest.raises(HeartbeatUnreadableError):
        HttpHeartbeatSource("ftp://example.invalid/x", 2).read()
