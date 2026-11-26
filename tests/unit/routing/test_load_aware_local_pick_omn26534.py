# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26534: the local tier picks the least busy healthy lab endpoint.

The customer-local check timed out queued behind lab traffic on one model
server while a peer serving the same model sat idle: the spread pick is a stable
hash of the correlation id and reads no load. The load snapshot is an INPUT to
the pick (as the quota snapshot is), read at the caller's boundary and passed in,
so the pick stays deterministic for a given snapshot.

Each test has a positive control: the same fixture with no snapshot spreads the
old way.
"""

from __future__ import annotations

import textwrap
import threading
from collections import Counter
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest

from omnimarket.handlers.routing.endpoint_load_pick import pick_least_busy_index
from omnimarket.handlers.routing.endpoint_load_reader import (
    LOAD_READ_TIMEOUT_SECONDS,
    endpoint_load_targets,
    metrics_url,
    parse_waiting_count,
    read_endpoint_load,
)
from omnimarket.models.delegation.model_endpoint_load import ModelEndpointLoad
from omnimarket.models.delegation.model_endpoint_load_snapshot import (
    ModelEndpointLoadSnapshot,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.unit

_RUNG = "local-heavy-reasoning"
_PEER = "local-studio-planner"
_RUNG_URL = "http://198.51.100.10:8000/v1/chat/completions"
_PEER_URL = "http://198.51.100.20:8000/v1/chat/completions"
_AS_OF = datetime(2026, 11, 23, 14, 0, tzinfo=UTC)

_TIERS_YAML = textwrap.dedent(
    f"""\
    tiers:
      - name: local
        cost_per_1k_tokens: 0.0
        models:
          - id: Qwen3.8-27B
            backend_id: {_RUNG}
            max_context_tokens: 65536
            use_for: [document, research, review]
            fast_path_threshold_tokens: 65536
        eval_before_accept: false
        max_retries: 0
      - name: cheap_cloud
        cost_per_1k_tokens: 0.002
        models:
          - id: cloud-model
            backend_id: cloud-x
            max_context_tokens: 65536
            use_for: [document, research, review]
        eval_before_accept: false
        max_retries: 0
    """
)


def _bifrost_yaml(rung_url: str = _RUNG_URL, peer_url: str = _PEER_URL) -> str:
    return textwrap.dedent(
        f"""\
        config_version: "1.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
          - backend_id: {_RUNG}
            provider: local
            endpoint_url: "{rung_url}"
            model_name: Qwen3.8-27B
            max_tokens: 4096
            timeout_ms: 30000
            tier: local
            capabilities: [document]
          - backend_id: {_PEER}
            provider: local
            endpoint_url: "{peer_url}"
            model_name: Qwen3.8-27B
            max_tokens: 4096
            timeout_ms: 30000
            tier: local
            capabilities: [document]
            placement:
              tier: local
              fallback_for: [{_RUNG}]
              max_context_tokens: 131072
              mode: spread
          - backend_id: cloud-x
            provider: gemini
            endpoint_url: "https://cloud.test/v1/chat/completions"
            model_name: cloud-model
            tier: frontier_api
            capabilities: [document]
        routing_rules:
          - rule_id: "7770b87c-9dc5-508d-9ee7-d7ac15acdfeb"
            priority: 10
            task_class: document
            task_class_contract_version: "1.0.0"
            backend_policy_version: "1.0.0"
            match_operation_types: [chat_completion]
            match_capabilities: [document]
            backend_ids: [{_RUNG}, cloud-x]
            fallback_policy:
              action: escalate_to_next_tier
              max_retries: 1
              on_exhaust: return_error
            shadow_policy_id: "9f0bcb8c-c33e-5016-a33a-f41a54b04c2b"
        default_backends:
          - {_RUNG}
        """
    )


_CLASS = """\
  {name}:
    gateway_exposure: public
    cloud_routing_policy: allowed
    pricing_ceiling_per_1k_tokens: 1.0
    definition_of_done:
      deterministic:
        - response_non_empty
    escalation_policy:
      max_escalations: 1
      tier_order:
        - local
        - cheap_cloud
"""
_TASK_CLASS_CONTRACT_YAML = "task_classes:\n" + "".join(
    _CLASS.format(name=name) for name in ("document", "research", "review")
)

_KEYS = tuple(uuid5(NAMESPACE_DNS, f"omn-26534-load-{i}") for i in range(60))


def _reset() -> None:
    routing._config = None
    routing._config_spread_peers = None
    routing._config_spread_weights = None
    routing._get_task_class_contract.cache_clear()
    routing._load_bifrost_endpoints.cache_clear()


def _bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rung_url: str = _RUNG_URL,
    peer_url: str = _PEER_URL,
) -> None:
    (tmp_path / "routing_tiers.yaml").write_text(_TIERS_YAML)
    (tmp_path / "bifrost_delegation.yaml").write_text(_bifrost_yaml(rung_url, peer_url))
    (tmp_path / "task_class_contracts.v1.yaml").write_text(_TASK_CLASS_CONTRACT_YAML)
    monkeypatch.setenv(
        "DELEGATION_ROUTING_TIERS_PATH", str(tmp_path / "routing_tiers.yaml")
    )
    monkeypatch.setenv(
        "BIFROST_CONTRACT_PATH", str(tmp_path / "bifrost_delegation.yaml")
    )
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(tmp_path / "no-overlay.yaml"))
    monkeypatch.setenv(
        "TASK_CLASS_CONTRACT_PATH", str(tmp_path / "task_class_contracts.v1.yaml")
    )
    _reset()


@pytest.fixture
def _bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    _bind(tmp_path, monkeypatch)
    yield
    _reset()


def _load(ref: str, waiting: int | None) -> ModelEndpointLoad:
    return ModelEndpointLoad(
        backend_ref=ref,
        waiting=waiting,
        reason="" if waiting is not None else "connection refused",
    )


def _snapshot(rung: int | None, peer: int | None) -> ModelEndpointLoadSnapshot:
    return ModelEndpointLoadSnapshot(
        as_of=_AS_OF, loads=(_load(_RUNG, rung), _load(_PEER, peer))
    )


def _request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Summarize: ok.",
        task_type="document",
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _picks(load: ModelEndpointLoadSnapshot | None) -> Counter[str]:
    return Counter(
        routing.delta(_request(key), load_state=load).selected_backend_ref
        for key in _KEYS
    )


# --- the pick, with a snapshot passed in ---------------------------------------


@pytest.mark.usefixtures("_bound")
def test_a_busy_first_endpoint_loses_to_an_idle_second() -> None:
    assert _picks(_snapshot(rung=4, peer=0)) == {_PEER: len(_KEYS)}
    # Positive control: with no snapshot the stable hash still sends some
    # requests to the first (busy) endpoint, which is the defect.
    assert _picks(None)[_RUNG] > 0


@pytest.mark.usefixtures("_bound")
def test_a_busy_second_endpoint_loses_to_an_idle_first() -> None:
    assert _picks(_snapshot(rung=0, peer=7)) == {_RUNG: len(_KEYS)}


@pytest.mark.usefixtures("_bound")
def test_an_unreachable_endpoint_is_skipped_even_when_the_other_is_busy() -> None:
    assert _picks(_snapshot(rung=None, peer=9)) == {_PEER: len(_KEYS)}
    assert _picks(_snapshot(rung=9, peer=None)) == {_RUNG: len(_KEYS)}


@pytest.mark.usefixtures("_bound")
def test_an_endpoint_the_snapshot_does_not_name_counts_as_unreadable() -> None:
    only_peer = ModelEndpointLoadSnapshot(as_of=_AS_OF, loads=(_load(_PEER, 3),))
    assert _picks(only_peer) == {_PEER: len(_KEYS)}


@pytest.mark.usefixtures("_bound")
@pytest.mark.parametrize("waiting", [0, 3])
def test_ties_resolve_in_declaration_order(waiting: int) -> None:
    assert _picks(_snapshot(rung=waiting, peer=waiting)) == {_RUNG: len(_KEYS)}


@pytest.mark.usefixtures("_bound")
def test_every_endpoint_unreadable_falls_back_to_the_existing_pick() -> None:
    blind = _snapshot(rung=None, peer=None)
    with_blind = [
        routing.delta(_request(key), load_state=blind).selected_backend_ref
        for key in _KEYS
    ]
    without = [routing.delta(_request(key)).selected_backend_ref for key in _KEYS]
    assert with_blind == without
    assert set(without) == {_RUNG, _PEER}


@pytest.mark.usefixtures("_bound")
def test_the_same_snapshot_gives_the_same_pick() -> None:
    snapshot = _snapshot(rung=2, peer=1)
    first = [
        routing.delta(_request(key), load_state=snapshot).selected_backend_ref
        for key in _KEYS
    ]
    assert first == [
        routing.delta(_request(key), load_state=snapshot).selected_backend_ref
        for key in _KEYS
    ]


@pytest.mark.usefixtures("_bound")
def test_the_snapshot_is_recorded_in_the_decision() -> None:
    decision = routing.delta(_request(_KEYS[0]), load_state=_snapshot(rung=4, peer=0))
    assert f"{_RUNG}=4" in decision.rationale
    assert f"{_PEER}=0" in decision.rationale
    assert decision.selected_backend_ref == _PEER
    # Positive control: a decision taken with no snapshot names no load.
    assert "waiting" not in routing.delta(_request(_KEYS[0])).rationale


@pytest.mark.usefixtures("_bound")
def test_the_in_process_path_picks_by_load_too() -> None:
    picks = Counter(
        routing.backend_id_for_tier(
            "local",
            "document",
            spread_key=str(key),
            prompt="Summarize: ok.",
            load_state=_snapshot(rung=5, peer=0),
        )
        for key in _KEYS
    )
    assert picks == {_PEER: len(_KEYS)}


@pytest.mark.usefixtures("_bound")
def test_the_customer_local_path_reads_the_load_for_its_initial_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
        port_local_delegation_dispatch as port,
    )

    dispatcher = object.__new__(port.LocalDelegationDispatchPort)

    def _initial_backends() -> Counter[str]:
        return Counter(
            dispatcher._resolve_initial_backend(
                "document", spread_key=str(key)
            ).backend_id
            for key in _KEYS
        )

    token = port._ACTIVE_PROMPT.set("Summarize: ok.")
    try:
        monkeypatch.setattr(
            port, "read_spread_load_snapshot", lambda: _snapshot(rung=5, peer=0)
        )
        assert _initial_backends() == {_PEER: len(_KEYS)}
        # Positive control: with no reading the same keys still land on the first.
        monkeypatch.setattr(port, "read_spread_load_snapshot", lambda: None)
        assert _initial_backends()[_RUNG] > 0
    finally:
        port._ACTIVE_PROMPT.reset(token)


# --- the pure pick -------------------------------------------------------------


def test_the_pure_pick_returns_none_with_no_usable_reading() -> None:
    assert pick_least_busy_index((_RUNG, _PEER), None) is None
    assert pick_least_busy_index((_RUNG, _PEER), _snapshot(None, None)) is None
    assert pick_least_busy_index((), _snapshot(0, 0)) is None


def test_the_pure_pick_takes_the_least_waiting_in_declaration_order() -> None:
    assert pick_least_busy_index((_RUNG, _PEER), _snapshot(2, 1)) == 1
    assert pick_least_busy_index((_PEER, _RUNG), _snapshot(2, 2)) == 0


# --- the reader ----------------------------------------------------------------

_VLLM_BODY = (
    "# HELP vllm:num_requests_running Number of requests in model execution batches.\n"
    'vllm:num_requests_running{engine="0",model_name="Qwen3.8-27B"} 4.0\n'
    "# HELP vllm:num_requests_waiting Number of requests waiting to be processed.\n"
    'vllm:num_requests_waiting{engine="0",model_name="Qwen3.8-27B"} 3.0\n'
)
_LLAMACPP_BODY = (
    "# TYPE llamacpp:requests_processing gauge\n"
    "llamacpp:requests_processing 1\n"
    "# TYPE llamacpp:requests_deferred gauge\n"
    "llamacpp:requests_deferred 2\n"
)


def test_a_vllm_scrape_reads_requests_waiting() -> None:
    assert parse_waiting_count(_VLLM_BODY) == 3


def test_a_vllm_scrape_sums_the_label_sets() -> None:
    body = (
        'vllm:num_requests_waiting{engine="0"} 1.0\n'
        'vllm:num_requests_waiting{engine="1"} 2.0\n'
    )
    assert parse_waiting_count(body) == 3


def test_a_llamacpp_scrape_reads_requests_deferred() -> None:
    assert parse_waiting_count(_LLAMACPP_BODY) == 2


@pytest.mark.parametrize("body", ["", "# nothing\nsomething_else 4\n", "<html></html>"])
def test_a_scrape_with_no_waiting_counter_is_unreadable(body: str) -> None:
    assert parse_waiting_count(body) is None


def test_the_metrics_url_is_the_endpoint_origin() -> None:
    assert metrics_url(_RUNG_URL) == "http://198.51.100.10:8000/metrics"
    assert metrics_url("http://a.test/v1/chat/completions") == "http://a.test/metrics"


@pytest.mark.usefixtures("_bound")
def test_targets_are_every_spread_group_member_taken_from_the_declaration() -> None:
    targets = endpoint_load_targets(
        routing._spread_peers_for(routing._get_config()),
        routing._load_bifrost_endpoints(),
    )
    assert targets == {_RUNG: _RUNG_URL, _PEER: _PEER_URL}


def test_no_spread_group_means_nothing_to_read() -> None:
    assert endpoint_load_targets({}, {}) == {}
    assert read_endpoint_load({}, as_of=_AS_OF).loads == ()


@contextmanager
def _metrics_server(status: int, body: str) -> Iterator[str]:
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = body.encode()
            self.send_response(status if self.path == "/metrics" else 404)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("localhost", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield f"http://{host!s}:{port}/v1/chat/completions"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_reader_scrapes_each_endpoint_over_http() -> None:
    with (
        _metrics_server(200, _VLLM_BODY) as busy,
        _metrics_server(200, _LLAMACPP_BODY) as llama,
    ):
        snapshot = read_endpoint_load({_RUNG: busy, _PEER: llama}, as_of=_AS_OF)
    assert [(load.backend_ref, load.waiting) for load in snapshot.loads] == [
        (_RUNG, 3),
        (_PEER, 2),
    ]
    assert snapshot.as_of == _AS_OF


def test_the_reader_marks_an_unhealthy_or_unreachable_endpoint_unreadable() -> None:
    with (
        _metrics_server(500, _VLLM_BODY) as erroring,
        _metrics_server(200, "nothing useful\n") as no_counter,
    ):
        snapshot = read_endpoint_load(
            {"erroring": erroring, "no-counter": no_counter}, as_of=_AS_OF
        )
    # A server that was up and has since closed its port refuses the connection.
    with _metrics_server(200, _VLLM_BODY) as closed:
        pass
    refused = read_endpoint_load({"closed": closed}, as_of=_AS_OF)
    waiting = {load.backend_ref: load.waiting for load in snapshot.loads}
    assert waiting == {"erroring": None, "no-counter": None}
    assert [load.waiting for load in refused.loads] == [None]
    assert all(load.reason for load in (*snapshot.loads, *refused.loads))


def test_the_reader_does_not_raise_the_read_timeout_past_a_few_seconds() -> None:
    assert 0 < LOAD_READ_TIMEOUT_SECONDS <= 5


def test_a_busy_first_server_loses_to_an_idle_second_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        _metrics_server(200, _VLLM_BODY) as busy,
        _metrics_server(200, 'vllm:num_requests_waiting{engine="0"} 0.0\n') as idle,
    ):
        _bind(tmp_path, monkeypatch, rung_url=busy, peer_url=idle)
        try:
            targets = endpoint_load_targets(
                routing._spread_peers_for(routing._get_config()),
                routing._load_bifrost_endpoints(),
            )
            snapshot = read_endpoint_load(targets, as_of=_AS_OF)
            picks = Counter(
                routing.delta(_request(key), load_state=snapshot).selected_backend_ref
                for key in _KEYS
            )
        finally:
            _reset()
    assert picks == {_PEER: len(_KEYS)}
