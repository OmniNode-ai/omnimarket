# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-23387): no stop request may leave the merge-queue runners on fewer than two hosts."""

from __future__ import annotations

from datetime import timedelta
from importlib.resources import files
from typing import Any

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_stop_idle_proof_compute.handlers import (
    HandlerRunnerStopIdleProof,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.handlers.handler_runner_stop_idle_proof import (
    SUBJECT_QUEUE_POOL_HOSTS,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.models import (
    ModelQueuePoolProof,
    ModelRunnerStopIdleProofRequest,
    ModelRunnerStopIdleProofResult,
    ModelRunnerStopIdleProofSettings,
)
from omnimarket.nodes.node_runner_stop_idle_proof_compute.tests.support import (
    FIRST_READ,
    GAP_SECONDS,
    idle_read,
)

_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN
H051 = tuple(f"hp051-ci-mq-runner-{i}" for i in range(1, 5))
H053 = tuple(f"hp053-ci-mq-runner-{i}" for i in range(1, 5))


def _settings(**over: Any) -> ModelRunnerStopIdleProofSettings:
    loaded: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_runner_stop_idle_proof_compute")
        .joinpath("overlay.yaml")
        .read_text()
    )
    return ModelRunnerStopIdleProofSettings.model_validate(
        {
            **loaded,
            "stop_set_labels": ("stop-label-a",),
            "proof_group_ids": (3, 7),
            **over,
        }
    )


def _request(
    names: tuple[str, ...],
    online: tuple[str, ...] | None,
    settings: ModelRunnerStopIdleProofSettings | None = None,
) -> ModelRunnerStopIdleProofRequest:
    return ModelRunnerStopIdleProofRequest(
        correlation_id="corr-queue-hosts",
        now=FIRST_READ + timedelta(seconds=90),
        settings=settings or _settings(),
        reads=(idle_read(0), idle_read(GAP_SECONDS)),
        stop_request_names=names,
        queue_pool_proof=ModelQueuePoolProof(
            read_at=FIRST_READ + timedelta(seconds=60), verdict=_W, proven_run_id=9001
        ),
        queue_runners_online=online,
    )


def _subject(result: ModelRunnerStopIdleProofResult) -> Any:
    return next(s for s in result.subjects if s.name == SUBJECT_QUEUE_POOL_HOSTS)


def test_queue_pool_spans_two_hosts_after_stop_overlay_names_both_hosts() -> None:
    settings = _settings()

    assert {p.host for p in settings.queue_pool_hosts} == {"h051", "h053"}
    assert settings.queue_pool_min_hosts == 2


def test_queue_pool_spans_two_hosts_after_stop_refuses_stopping_a_whole_host() -> None:
    result = HandlerRunnerStopIdleProof().handle(_request(H051, H051 + H053))

    assert _subject(result).verdict is _N
    assert "would sit on 1 host(s), fewer than the 2" in _subject(result).reason
    assert result.refused_stop_names == H051
    assert result.verdict is _N


def test_queue_pool_spans_two_hosts_after_stop_allows_a_part_of_one_host() -> None:
    result = HandlerRunnerStopIdleProof().handle(_request(H051[:2], H051 + H053))

    assert _subject(result).verdict is _W
    assert result.refused_stop_names == ()


def test_queue_pool_spans_two_hosts_after_stop_refuses_when_the_other_host_is_already_down() -> (
    None
):
    result = HandlerRunnerStopIdleProof().handle(_request(H051[:1], H051 + H053[:0]))

    assert _subject(result).verdict is _N
    assert result.refused_stop_names == H051[:1]


def test_queue_pool_spans_two_hosts_after_stop_is_unknown_when_the_pool_was_not_read() -> (
    None
):
    result = HandlerRunnerStopIdleProof().handle(_request(H051[:2], None))

    assert _subject(result).verdict is _U
    assert result.refused_stop_names == H051[:2]


def test_queue_pool_spans_two_hosts_after_stop_ignores_a_stop_of_other_runners() -> (
    None
):
    result = HandlerRunnerStopIdleProof().handle(_request(("omninode-runner-3",), None))

    assert _subject(result).verdict is _W
    assert _subject(result).count == 0


def test_queue_pool_spans_two_hosts_after_stop_is_not_judged_with_no_hosts_declared() -> (
    None
):
    result = HandlerRunnerStopIdleProof().handle(
        _request(H051, H051 + H053, _settings(queue_pool_hosts=()))
    )

    assert SUBJECT_QUEUE_POOL_HOSTS not in {s.name for s in result.subjects}


def test_queue_pool_spans_two_hosts_after_stop_refuses_a_floor_below_two() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 2"):
        _settings(queue_pool_min_hosts=1)


class _HostFloorIgnored(HandlerRunnerStopIdleProof):
    """Injected break: the hosts left after the stop are never counted."""

    def handle(
        self, request: ModelRunnerStopIdleProofRequest
    ) -> ModelRunnerStopIdleProofResult:
        return super().handle(
            request.model_copy(
                update={
                    "settings": request.settings.model_copy(
                        update={"queue_pool_hosts": ()}
                    )
                }
            )
        )


def _assert_a_whole_host_stop_is_refused(handler: HandlerRunnerStopIdleProof) -> None:
    refused = handler.handle(_request(H051, H051 + H053))
    assert refused.refused_stop_names == H051
    assert handler.handle(_request(H051[:2], H051 + H053)).refused_stop_names == ()


def test_queue_pool_spans_two_hosts_after_stop_passes_for_the_real_handler() -> None:
    _assert_a_whole_host_stop_is_refused(HandlerRunnerStopIdleProof())


def test_queue_pool_spans_two_hosts_after_stop_turns_red_under_the_injected_break() -> (
    None
):
    with pytest.raises(AssertionError):
        _assert_a_whole_host_stop_is_refused(_HostFloorIgnored())
