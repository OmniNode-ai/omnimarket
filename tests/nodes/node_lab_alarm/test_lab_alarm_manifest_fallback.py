# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A manifest endpoint that does not answer is not a lag reading (OMN-22874).

The runtime serves its manifest only once its kernel is up, so the endpoint refuses or resets for minutes
while dev redeploys. Before this change one refused fetch read INDETERMINATE as ``derivation:manifest fetch
failed (URLError)``, indistinguishable from a lag that could not be read, and the floor row went red on the
reader's own fault.

Golden: a transient failure is retried, and an unreachable endpoint falls back to the last good manifest while
it is inside the overlay's bound, naming its age; the lag rule still runs on the broker's real readings and
still alarms on growth. Error: with no usable last good manifest the run is INDETERMINATE and says
``manifest-unreachable`` and that the lag was not read; a body that arrived and cannot be read never falls back;
the named break ``manifest-down`` reproduces the outage.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryLag,
    InMemoryManifest,
    InMemoryManifestCache,
    InMemoryRunRecords,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    FileManifestCache,
    HttpManifest,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm.protocols import (
    ManifestUnreachableError,
    ManifestUnreadableError,
)
from tests.nodes.node_lab_alarm.test_lab_alarm_chain_events import (
    _NODE,
    _TOPICS,
    _flat,
    _group,
    _manifest,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)


def _overlay(*, resilience: bool = True) -> ModelLabAlarmOverlay:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    if not resilience:
        del raw["manifest"]["resilience"]
    return ModelLabAlarmOverlay.model_validate(raw)


def _good_cache(age_seconds: int) -> InMemoryManifestCache:
    return InMemoryManifestCache((_manifest(), _NOW - timedelta(seconds=age_seconds)))


def _handler(
    *,
    lags: dict[str, int],
    previous: dict[str, int] | None,
    manifest: InMemoryManifest,
    cache: InMemoryManifestCache | None,
    resilience: bool = True,
) -> tuple[HandlerBrokerLag, InMemoryRunRecords, InMemorySamples]:
    records = InMemoryRunRecords()
    samples = InMemorySamples(previous)
    handler = HandlerBrokerLag(
        overlay=_overlay(resilience=resilience),
        manifest=manifest,
        lag=InMemoryLag(lags),
        samples=samples,
        records=records,
        clock=lambda: _NOW,
        manifest_cache=cache,
    )
    return handler, records, samples


def _down() -> InMemoryManifest:
    manifest = InMemoryManifest(_manifest())
    manifest.unreachable = "manifest fetch failed (URLError) after 3 attempts"
    return manifest


def _cmd(fault: EnumLabAlarmFault = EnumLabAlarmFault.NONE) -> ModelBrokerLagCommand:
    return ModelBrokerLagCommand(correlation_id="fb-1", fault=fault)


# --- the reader: a bounded retry over a real HTTP endpoint -------------------------------------------------


class _Flaky:
    """A real local HTTP server that drops the first ``drops`` connections the way a starting runtime does."""

    def __init__(self, drops: int, body: bytes) -> None:
        self.seen = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.seen += 1
                if outer.seen <= drops:
                    self.connection.close()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1/introspection/manifest"

    def __enter__(self) -> _Flaky:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_golden_a_blip_is_retried_and_the_manifest_is_read() -> None:
    sleeps: list[float] = []
    with _Flaky(drops=2, body=json.dumps(_manifest()).encode()) as server:
        manifest = HttpManifest(
            server.url,
            5.0,
            attempts=3,
            retry_delay_seconds=7.0,
            sleep=sleeps.append,
        ).read()
        assert server.seen == 3
    assert manifest["contracts"]
    assert sleeps == [7.0, 7.0]  # between attempts, never after the last


def test_error_a_single_attempt_reader_is_the_old_behaviour_and_names_unreachable() -> (
    None
):
    """The failing control: with no retry the same blip is unreachable, which is what read red on the floor."""
    with (
        _Flaky(drops=2, body=json.dumps(_manifest()).encode()) as server,
        pytest.raises(ManifestUnreachableError, match=r"after 1 attempt$"),
    ):
        HttpManifest(server.url, 5.0, attempts=1).read()


def test_error_an_endpoint_down_for_every_attempt_is_unreachable_naming_the_count() -> (
    None
):
    sleeps: list[float] = []
    with pytest.raises(
        ManifestUnreachableError,
        match=r"manifest fetch failed \(\w+\) after 3 attempts",
    ):
        HttpManifest(
            "http://127.0.0.1:1/v1/introspection/manifest",
            1.0,
            attempts=3,
            retry_delay_seconds=0.5,
            sleep=sleeps.append,
        ).read()
    assert sleeps == [0.5, 0.5]


def test_error_a_body_that_arrived_and_is_not_json_is_not_retried_and_not_unreachable() -> (
    None
):
    with _Flaky(drops=0, body=b"<html>starting</html>") as server:
        with pytest.raises(ManifestUnreadableError, match="not JSON") as raised:
            HttpManifest(server.url, 5.0, attempts=3).read()
        assert server.seen == 1
    assert not isinstance(raised.value, ManifestUnreachableError)


# --- the handler: fallback to the last good manifest, age named --------------------------------------------


def test_golden_chain_an_unreachable_manifest_falls_back_and_names_the_age() -> None:
    topics = load_topics()
    handler, records, samples = _handler(
        lags=_flat(7), previous=_flat(7), manifest=_down(), cache=_good_cache(600)
    )
    result = handler.handle(_cmd())
    events = events_for(result, topics)

    assert result.outcome is EnumBrokerLagOutcome.OK
    assert events[-1][0] == topics["broker_lag_run_completed"]
    assert result.declared_groups == tuple(sorted(_flat()))
    assert len(result.readings) == 2  # the broker was read, not skipped
    assert result.reasons[0].startswith(
        "manifest-stale:group names from the last good manifest, 600s old"
    )
    assert result.evidence.startswith("manifest-stale:")
    assert samples.sample == _flat(7)
    assert records.records[-1]["reports"][0]["evidence"].startswith("manifest-stale:")
    assert records.records[-1]["reports"][0]["outcome"] == "OK"


def test_error_chain_the_fallback_never_mutes_the_alarm() -> None:
    lags = _flat(5)
    first = _group("1.2.0", _TOPICS[0])
    lags[first] = 9
    handler, _records, _samples = _handler(
        lags=lags, previous=_flat(5), manifest=_down(), cache=_good_cache(120)
    )
    result = handler.handle(_cmd())
    events = events_for(result, load_topics())
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert result.alarms == (f"{first}: lag grew 5->9",)
    assert events[-1][0] == load_topics()["broker_lag_run_failed"]
    assert result.reasons[0].startswith("manifest-stale:")


def test_error_chain_no_last_good_manifest_reads_manifest_unreachable_and_lag_unread() -> (
    None
):
    handler, records, samples = _handler(
        lags=_flat(7),
        previous=_flat(7),
        manifest=_down(),
        cache=InMemoryManifestCache(None),
    )
    result = handler.handle(_cmd())
    events = events_for(result, load_topics())

    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert result.reasons == (
        "manifest-unreachable:manifest fetch failed (URLError) after 3 attempts; "
        "no last good manifest is kept; lag was not read",
    )
    assert not result.reasons[0].startswith("derivation:")
    assert "not a lag figure" in result.evidence
    assert result.readings == ()
    assert events[-1][0] == load_topics()["broker_lag_run_failed"]
    assert samples.sample == _flat(7)  # the baseline is left for the next tick
    recorded = records.records[-1]["reports"][0]
    assert recorded["outcome"] == "INDETERMINATE"
    assert recorded["evidence"].startswith("manifest-unreachable:")


def test_error_a_last_good_manifest_over_the_bound_is_not_used_and_says_so() -> None:
    handler, *_ = _handler(
        lags=_flat(7), previous=_flat(7), manifest=_down(), cache=_good_cache(3601)
    )
    result = handler.handle(_cmd())
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert (
        "the last good manifest is 3601s old, over the 3600s bound"
        in (result.reasons[0])
    )
    assert result.reasons[0].startswith("manifest-unreachable:")


def test_the_bound_itself_is_still_inside() -> None:
    handler, *_ = _handler(
        lags=_flat(7), previous=_flat(7), manifest=_down(), cache=_good_cache(3600)
    )
    assert handler.handle(_cmd()).outcome is EnumBrokerLagOutcome.OK


def test_without_the_overlay_stanza_an_unreachable_manifest_is_the_typed_indeterminate() -> (
    None
):
    """No resilience declared: no fallback even with a cache wired, only the clearer name."""
    handler, *_ = _handler(
        lags=_flat(7),
        previous=_flat(7),
        manifest=_down(),
        cache=_good_cache(10),
        resilience=False,
    )
    result = handler.handle(_cmd())
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert result.reasons[0].startswith("manifest-unreachable:")


def test_error_a_manifest_that_arrived_and_cannot_be_read_never_falls_back() -> None:
    manifest = InMemoryManifest(_manifest())
    manifest.unreadable = "manifest body is not JSON"
    handler, *_ = _handler(
        lags=_flat(7), previous=_flat(7), manifest=manifest, cache=_good_cache(10)
    )
    result = handler.handle(_cmd())
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert result.reasons == ("derivation:manifest body is not JSON",)


def test_a_reachable_manifest_is_kept_as_the_last_good_and_only_the_declared_nodes() -> (
    None
):
    cache = InMemoryManifestCache(None)
    handler, *_ = _handler(
        lags=_flat(7),
        previous=_flat(7),
        manifest=InMemoryManifest(_manifest()),
        cache=cache,
    )
    assert handler.handle(_cmd()).outcome is EnumBrokerLagOutcome.OK
    assert cache.entry is not None
    kept, fetched_at = cache.entry
    assert fetched_at == _NOW
    assert [c["name"] for c in kept["contracts"]] == ["projection_example"]


def test_error_chain_the_named_break_manifest_down_reproduces_the_outage() -> None:
    """The injected break: the endpoint does not answer. With a good copy it falls back; without, it is named."""
    handler, _records, samples = _handler(
        lags=_flat(7),
        previous=_flat(7),
        manifest=InMemoryManifest(_manifest()),
        cache=InMemoryManifestCache(None),
    )
    result = handler.handle(_cmd(EnumLabAlarmFault.MANIFEST_DOWN))
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert (
        "manifest-unreachable:manifest fetch failed (injected manifest-down)"
        in (result.reasons[0])
    )
    assert samples.sample == _flat(7)

    handler, *_ = _handler(
        lags=_flat(7),
        previous=_flat(7),
        manifest=InMemoryManifest(_manifest()),
        cache=_good_cache(30),
    )
    result = handler.handle(_cmd(EnumLabAlarmFault.MANIFEST_DOWN))
    assert result.outcome is EnumBrokerLagOutcome.OK
    assert result.reasons[0].startswith("manifest-stale:")
    assert result.sample_kept is False  # a drill never moves the baseline


def test_a_drill_never_overwrites_the_last_good_manifest() -> None:
    cache = _good_cache(500)
    before = cache.entry
    handler, *_ = _handler(
        lags=_flat(7),
        previous=_flat(7),
        manifest=InMemoryManifest(_manifest()),
        cache=cache,
    )
    handler.handle(_cmd(EnumLabAlarmFault.PLANTED_LAG))
    assert cache.entry == before


def test_the_file_cache_round_trips_and_ignores_a_damaged_or_naive_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "manifest-last-good.json"
    store = FileManifestCache(path)
    assert store.load() is None
    store.save({"contracts": [{"name": "a"}]}, _NOW)
    assert store.load() == ({"contracts": [{"name": "a"}]}, _NOW)
    path.write_text(
        json.dumps(
            {"fetched_at": "2026-11-08T01:00:00", "manifest": {"contracts": []}}
        ),
        encoding="utf-8",
    )
    loaded = store.load()
    assert loaded is not None
    assert loaded[1] == _NOW  # a naive stamp is read as UTC
    path.write_text("not json", encoding="utf-8")
    assert store.load() is None
    path.write_text(json.dumps({"fetched_at": "x", "manifest": []}), encoding="utf-8")
    assert store.load() is None


def test_an_unwritable_cache_never_fails_the_run(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    FileManifestCache(blocker / "child" / "m.json").save({"contracts": []}, _NOW)


def test_the_overlay_stanza_is_optional_and_each_key_of_it_is_required() -> None:
    assert _overlay(resilience=False).manifest.resilience is None
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    for key in raw["manifest"]["resilience"]:
        broken = json.loads(json.dumps(raw))
        del broken["manifest"]["resilience"][key]
        with pytest.raises(ValueError, match=key):
            ModelLabAlarmOverlay.model_validate(broken)
    broken = json.loads(json.dumps(raw))
    broken["manifest"]["resilience"]["attempts"] = 0
    with pytest.raises(ValueError, match="attempts"):
        ModelLabAlarmOverlay.model_validate(broken)
