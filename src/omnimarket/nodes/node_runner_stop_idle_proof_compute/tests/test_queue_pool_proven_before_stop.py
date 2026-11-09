# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-23156): the queue runners are not on the stop list until the replacement pool is proven, and a stop request before it is refused."""

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
    SUBJECT_QUEUE_POOL_PROVEN,
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
QUEUE_NAMES = ("hp051-ci-mq-runner-1", "hp051-ci-mq-runner-4")
NOW_OFFSET = 90


def _overlay() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_runner_stop_idle_proof_compute")
        .joinpath("overlay.yaml")
        .read_text()
    )
    return loaded


def _settings() -> ModelRunnerStopIdleProofSettings:
    return ModelRunnerStopIdleProofSettings.model_validate(
        {
            **_overlay(),
            "stop_set_labels": ("stop-label-a",),
            "proof_group_ids": (3, 7),
        }
    )


def _proof(
    verdict: EnumReadingVerdict = _W, run_id: int | None = 9001, age_seconds: int = 30
) -> ModelQueuePoolProof:
    return ModelQueuePoolProof(
        read_at=FIRST_READ + timedelta(seconds=NOW_OFFSET - age_seconds),
        verdict=verdict,
        proven_run_id=run_id,
    )


def _request(
    names: tuple[str, ...] = QUEUE_NAMES, proof: ModelQueuePoolProof | None = None
) -> ModelRunnerStopIdleProofRequest:
    return ModelRunnerStopIdleProofRequest(
        correlation_id="corr-queue-pool",
        now=FIRST_READ + timedelta(seconds=NOW_OFFSET),
        settings=_settings(),
        reads=(idle_read(0), idle_read(GAP_SECONDS)),
        stop_request_names=names,
        queue_pool_proof=proof,
    )


def _subject(result: ModelRunnerStopIdleProofResult) -> Any:
    return next(s for s in result.subjects if s.name == SUBJECT_QUEUE_POOL_PROVEN)


def test_queue_pool_proven_before_stop_overlay_names_the_queue_runners() -> None:
    assert _settings().queue_pool_stop_name_prefixes == ("hp051-ci-mq-runner",)


def test_queue_pool_proven_before_stop_refuses_a_stop_with_no_proof_read() -> None:
    result = HandlerRunnerStopIdleProof().handle(_request())

    assert _subject(result).verdict is _U
    assert result.verdict.blocks is True
    assert result.refused_stop_names == QUEUE_NAMES
    assert "stop refused" in _subject(result).reason


@pytest.mark.parametrize("verdict", [_N, _U])
def test_queue_pool_proven_before_stop_refuses_a_stop_while_the_pool_does_not_read_working(
    verdict: EnumReadingVerdict,
) -> None:
    result = HandlerRunnerStopIdleProof().handle(_request(proof=_proof(verdict)))

    assert _subject(result).verdict is _N
    assert result.verdict is _N
    assert result.refused_stop_names == QUEUE_NAMES


def test_queue_pool_proven_before_stop_refuses_a_working_reading_with_no_proven_run() -> (
    None
):
    result = HandlerRunnerStopIdleProof().handle(_request(proof=_proof(run_id=None)))

    assert _subject(result).verdict is _N
    assert result.refused_stop_names == QUEUE_NAMES


def test_queue_pool_proven_before_stop_refuses_a_stale_proof() -> None:
    limit = _settings().queue_pool_proof_max_age_seconds
    result = HandlerRunnerStopIdleProof().handle(
        _request(proof=_proof(age_seconds=limit + 1))
    )

    assert _subject(result).verdict is _U
    assert result.refused_stop_names == QUEUE_NAMES


def test_queue_pool_proven_before_stop_allows_the_stop_after_the_pool_is_proven() -> (
    None
):
    result = HandlerRunnerStopIdleProof().handle(_request(proof=_proof()))

    assert _subject(result).verdict is _W
    assert result.refused_stop_names == ()
    assert result.verdict is _W
    assert "merge-group run 9001" in _subject(result).reason


def test_queue_pool_proven_before_stop_ignores_a_request_with_no_queue_runner() -> None:
    result = HandlerRunnerStopIdleProof().handle(_request(names=("other-runner-1",)))

    assert _subject(result).verdict is _W
    assert result.refused_stop_names == ()


def test_queue_pool_proven_before_stop_matches_the_name_in_any_case() -> None:
    result = HandlerRunnerStopIdleProof().handle(
        _request(names=("HP051-CI-MQ-RUNNER-2",))
    )

    assert result.refused_stop_names == ("HP051-CI-MQ-RUNNER-2",)


class _ProofIgnored(HandlerRunnerStopIdleProof):
    """Injected break: the pool's proof is taken as given whatever it reads."""

    def handle(
        self, request: ModelRunnerStopIdleProofRequest
    ) -> ModelRunnerStopIdleProofResult:
        return super().handle(
            request.model_copy(update={"queue_pool_proof": _proof(_W, 1)})
        )


class _QueueNamesNotJudged(HandlerRunnerStopIdleProof):
    """Injected break: no queue name is ever on the stop request."""

    def handle(
        self, request: ModelRunnerStopIdleProofRequest
    ) -> ModelRunnerStopIdleProofResult:
        return super().handle(request.model_copy(update={"stop_request_names": ()}))


def _assert_refused_before_proof(handler: HandlerRunnerStopIdleProof) -> None:
    for proof in (None, _proof(_N), _proof(_U), _proof(run_id=None)):
        result = handler.handle(_request(proof=proof))
        assert result.verdict is not _W
        assert result.refused_stop_names == QUEUE_NAMES
    assert handler.handle(_request(proof=_proof())).refused_stop_names == ()


def test_queue_pool_proven_before_stop_passes_for_the_real_handler() -> None:
    _assert_refused_before_proof(HandlerRunnerStopIdleProof())


@pytest.mark.parametrize("broken", [_ProofIgnored, _QueueNamesNotJudged])
def test_queue_pool_proven_before_stop_turns_red_under_the_injected_break(
    broken: type[HandlerRunnerStopIdleProof],
) -> None:
    with pytest.raises(AssertionError):
        _assert_refused_before_proof(broken())
