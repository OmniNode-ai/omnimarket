# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A backend whose host refuses connections is skipped by ordinary routing (OMN-20910).

Measured on the dev lane over 24 hours: the ``local-omnipc2-chat`` rung accepted
0 of 10 summarization attempts, every one ``provider_call_failed`` with
``[Errno 113] No route to host`` from its host. Routing kept choosing it as a
spread peer: the only memory of a transport failure was the per-workflow
``transport_failed_backend_refs`` set, which every new delegation starts empty,
and a call to a local (unmetered) host emitted no observation at all, so the
durable provider quota state never heard of it.

The fix reuses that durable state. A connection-level failure on any host is
observed as a timed, provider-wide block on the host's own quota key; the
backend is not routable for ordinary work until the block lifts, when the next
attempt is the health probe. A pinned run (and a smoke run is one) still reaches
the backend, so the host can be proved back before ordinary traffic returns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.enums.enum_routing_exclusion import EnumRoutingExclusionReason
from omnimarket.events.provider_quota import (
    EnumProviderQuotaOutcome,
    EnumProviderQuotaSource,
)
from omnimarket.inference.provider_quota_observation import (
    HOST_UNREACHABLE_COOLDOWN,
    build_quota_observation,
    observe_failed_call,
)
from omnimarket.inference.provider_quota_state import (
    HOST_UNREACHABLE_DISPOSITION,
    ModelProviderQuotaBlock,
    ModelProviderQuotaSnapshot,
)
from omnimarket.models.delegation.wire import ModelTierModel
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    BifrostBackendRef,
    _backend_routable,
    _candidate_exclusion_reason,
    _select_model_for_task,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 26, 3, 30, tzinfo=UTC)
_DEAD_URL = "http://dead-host.test:8000/v1/chat/completions"
_LIVE_URL = "http://live-host.test:8000/v1/chat/completions"
_NO_ROUTE = "provider call failed: [Errno 113] No route to host from dead-host.test"


def _backend(url: str) -> BifrostBackendRef:
    return BifrostBackendRef(
        endpoint_url=url,
        model_name="qwen3.8",
        timeout_ms=60000,
        max_tokens=8192,
        api_key_ref=None,
    )


def _snapshot_after_connection_failure(
    url: str = _DEAD_URL, *, error: str = _NO_ROUTE
) -> ModelProviderQuotaSnapshot:
    observation, _ = observe_failed_call(
        tenant_id=None,
        endpoint_url=url,
        api_key_ref=None,
        model_name="qwen3.8",
        error_message=error,
        observed_at=_NOW,
        latency_ms=3000,
        source=EnumProviderQuotaSource.RUNTIME_ORCHESTRATOR,
    )
    return ModelProviderQuotaSnapshot.empty(as_of=_NOW).with_observation(observation)


class TestConnectionFailureIsObserved:
    def test_no_route_to_a_local_host_records_a_timed_host_block(self) -> None:
        observation = build_quota_observation(
            tenant_id=None,
            endpoint_url=_DEAD_URL,
            api_key_ref=None,
            model_name="qwen3.8",
            succeeded=False,
            observed_at=_NOW,
            latency_ms=0,
            source=EnumProviderQuotaSource.RUNTIME_ORCHESTRATOR,
            error_message=_NO_ROUTE,
        )
        assert observation is not None
        assert observation.outcome is EnumProviderQuotaOutcome.LIMIT_HIT
        assert observation.disposition == HOST_UNREACHABLE_DISPOSITION
        assert observation.provider_id == "host:dead-host.test:8000"
        assert observation.blocked_until == _NOW + HOST_UNREACHABLE_COOLDOWN
        assert not observation.blocked_indefinitely

    @pytest.mark.parametrize(
        "error",
        [
            "provider call failed: [Errno 111] Connection refused",
            "httpx.ConnectError: All connection attempts failed",
            "httpx.ConnectTimeout: timed out",
            "[Errno -2] Name or service not known",
            "[Errno 101] Network is unreachable",
        ],
    )
    def test_each_connection_level_failure_blocks_the_host(self, error: str) -> None:
        snapshot = _snapshot_after_connection_failure(error=error)
        assert _backend_routable(_backend(_DEAD_URL), quota_state=snapshot) is False

    @pytest.mark.parametrize(
        "error",
        [
            "provider HTTP 500 Internal Server Error for http://x; response_body={}",
            "provider HTTP 503 Service Unavailable for http://x",
            "read timed out",
            "quality_gate_failed",
        ],
    )
    def test_a_host_that_answered_or_was_merely_slow_is_not_blocked(
        self, error: str
    ) -> None:
        snapshot = _snapshot_after_connection_failure(error=error)
        assert _backend_routable(_backend(_DEAD_URL), quota_state=snapshot) is True

    def test_a_refusal_on_one_port_does_not_bar_a_service_on_another(self) -> None:
        snapshot = _snapshot_after_connection_failure(
            error="[Errno 111] Connection refused"
        )
        other_port = "http://dead-host.test:8001/v1/chat/completions"
        assert _backend_routable(_backend(_DEAD_URL), quota_state=snapshot) is False
        assert _backend_routable(_backend(other_port), quota_state=snapshot) is True

    def test_a_successful_local_call_emits_nothing(self) -> None:
        assert (
            build_quota_observation(
                tenant_id=None,
                endpoint_url=_LIVE_URL,
                api_key_ref=None,
                model_name="qwen3.8",
                succeeded=True,
                observed_at=_NOW,
                latency_ms=10,
                source=EnumProviderQuotaSource.RUNTIME_ORCHESTRATOR,
            )
            is None
        )


class TestRoutingSkipsTheUnreachableHost:
    @pytest.mark.parametrize("quota_first", [False, True])
    def test_host_wide_quota_still_binds_a_pin_beside_a_port_failure(
        self, quota_first: bool
    ) -> None:
        snapshot = _snapshot_after_connection_failure()
        quota = ModelProviderQuotaBlock(
            credential_ref="unauthenticated",
            provider_id="host:dead-host.test",
            model_scope="*",
            disposition="disable_until_billing",
            blocked_indefinitely=True,
        )
        blocks = (quota, *snapshot.blocks) if quota_first else (*snapshot.blocks, quota)
        snapshot = snapshot.model_copy(update={"blocks": blocks})
        for url in (_DEAD_URL, "http://dead-host.test:8001/v1/chat/completions"):
            assert _backend_routable(_backend(url), quota_state=snapshot) is False
            assert (
                _backend_routable(
                    _backend(url), quota_state=snapshot, explicit_pin=True
                )
                is False
            )

    def test_ordinary_routing_skips_it_and_leaves_its_sibling(self) -> None:
        snapshot = _snapshot_after_connection_failure()
        assert _backend_routable(_backend(_DEAD_URL), quota_state=snapshot) is False
        assert _backend_routable(_backend(_LIVE_URL), quota_state=snapshot) is True

    def test_a_pinned_run_still_reaches_it(self) -> None:
        snapshot = _snapshot_after_connection_failure()
        assert (
            _backend_routable(
                _backend(_DEAD_URL), quota_state=snapshot, explicit_pin=True
            )
            is True
        )

    def test_the_host_returns_when_the_cooldown_ends_so_the_next_call_probes_it(
        self,
    ) -> None:
        snapshot = _snapshot_after_connection_failure()
        later = snapshot.model_copy(
            update={"as_of": _NOW + HOST_UNREACHABLE_COOLDOWN + timedelta(seconds=1)}
        )
        assert _backend_routable(_backend(_DEAD_URL), quota_state=later) is True

    def test_a_failed_probe_blocks_it_again(self) -> None:
        probe_at = _NOW + HOST_UNREACHABLE_COOLDOWN + timedelta(seconds=5)
        observation, _ = observe_failed_call(
            tenant_id=None,
            endpoint_url=_DEAD_URL,
            api_key_ref=None,
            model_name="qwen3.8",
            error_message=_NO_ROUTE,
            observed_at=probe_at,
            latency_ms=0,
            source=EnumProviderQuotaSource.RUNTIME_ORCHESTRATOR,
        )
        again = _snapshot_after_connection_failure().model_copy(
            update={"as_of": probe_at}
        )
        again = again.with_observation(observation)
        assert _backend_routable(_backend(_DEAD_URL), quota_state=again) is False

    def test_a_spread_peer_on_the_dead_host_is_never_picked(self) -> None:
        rung = ModelTierModel(
            id="qwen3.8",
            backend_ref="local-coder",
            max_context_tokens=32768,
            use_for=("summarization",),
        )
        peer = ModelTierModel(
            id="qwen3.8",
            backend_ref="local-omnipc2-chat",
            max_context_tokens=32768,
            use_for=("summarization",),
        )
        backends = {
            "local-coder": _backend(_LIVE_URL),
            "local-omnipc2-chat": _backend(_DEAD_URL),
        }
        snapshot = _snapshot_after_connection_failure()
        picks = {
            _select_model_for_task(
                (rung, peer),
                "summarization",
                100,
                backends,
                spread_key=f"correlation-{n}",
                spread_peers={"local-coder": ("local-omnipc2-chat",)},
                spread_member_weights={"local-omnipc2-chat": 1.0},
                quota_state=snapshot,
            ).backend_ref
            for n in range(200)
        }
        assert picks == {"local-coder"}

        # Control: with no health fact the same keys do reach the peer.
        healthy = ModelProviderQuotaSnapshot.empty(as_of=_NOW)
        control = {
            _select_model_for_task(
                (rung, peer),
                "summarization",
                100,
                backends,
                spread_key=f"correlation-{n}",
                spread_peers={"local-coder": ("local-omnipc2-chat",)},
                spread_member_weights={"local-omnipc2-chat": 1.0},
                quota_state=healthy,
            ).backend_ref
            for n in range(200)
        }
        assert control == {"local-coder", "local-omnipc2-chat"}

    def test_the_exclusion_report_names_the_host_not_a_quota(self) -> None:
        peer = ModelTierModel(
            id="qwen3.8",
            backend_ref="local-omnipc2-chat",
            max_context_tokens=32768,
            use_for=("summarization",),
        )
        reason, _ = _candidate_exclusion_reason(
            peer,
            task_type="summarization",
            estimated_tokens=100,
            bifrost_backends={"local-omnipc2-chat": _backend(_DEAD_URL)},
            excluded_backend_refs=frozenset(),
            quota_state=_snapshot_after_connection_failure(),
        )
        assert reason is EnumRoutingExclusionReason.BACKEND_HOST_UNREACHABLE
