# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration tests for the OCC observation chain (OMN-14906).

These tests exist because the existing unit/golden-chain suites stub
``_read_occ_preflight_eligible`` **wholesale** (see
``tests/unit/nodes/node_occ_attestation_observe/test_attestation_observe.py`` and
``tests/nodes/node_occ_attestation_observe/test_golden_chain_occ_attestation_observe.py``).
A stub of the method under test cannot catch a mismatch *inside* that method,
which is exactly how the ``/commits/{sha}/check-runs`` response-shape defect
survived: the endpoint returns a JSON **object**
(``{"total_count": N, "check_runs": [...]}``) but the handler called
``rest_json_array``, whose list-only contract raised ``GitHubApiError``, which the
caller swallowed to ``False`` — making ``occ_preflight_eligible`` structurally
unreachable and therefore N=10 unreachable.

So these tests stub the **HTTP transport** (``urllib.request.urlopen``) and run
the real ``_read_occ_preflight_eligible`` → real ``omnimarket.github_api`` →
real ``json`` decode path against the real GitHub payload shape.

They also cover the two remaining links in the chain:

* the read side must **ERROR** on an ABSENT observation store rather than
  reporting ``raw_record_count=0`` (an absent store is indistinguishable from an
  empty one — the ``optional input that silently skips`` failure mode); and
* the source → projection → window composition must emit a **concrete streak**.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from omnimarket.events.occ_autoauthor import ModelOccAutoauthorObservation
from omnimarket.events.occ_observation_record import ModelOccObservationRecord
from omnimarket.events.occ_observation_store import (
    OCC_OBSERVATIONS_ROOT,
    occ_observation_record_relpath,
    render_occ_observation_record,
)
from omnimarket.nodes.node_occ_attestation_observe.handlers.handler_occ_attestation_observe import (
    HandlerOccAttestationObserve,
)
from omnimarket.nodes.node_occ_autoauthor_window.handlers.handler_occ_autoauthor_window import (
    HandlerOccAutoauthorWindow,
)
from omnimarket.nodes.node_occ_autoauthor_window.models.model_occ_autoauthor_window_request import (
    ModelOccAutoauthorWindowRequest,
)
from omnimarket.nodes.node_occ_observation_source_effect.handlers.handler_occ_observation_source_effect import (
    HandlerOccObservationSourceEffect,
)
from omnimarket.nodes.node_occ_observation_source_effect.models.model_occ_observation_source_effect_request import (
    ModelOccObservationSourceEffectRequest,
)

_PREFLIGHT_CHECK = "occ-preflight / eligibility"
_HEAD_SHA = "1234567890abcdef1234567890abcdef12345678"
_REPO = "OmniNode-ai/omnimarket"


# --------------------------------------------------------------------------- #
# HTTP transport double — the REAL github_api code runs on top of this        #
# --------------------------------------------------------------------------- #


class _FakeResponse(io.BytesIO):
    """Minimal context-manager stand-in for an ``http.client.HTTPResponse``."""

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]
) -> list[str]:
    """Serve ``pages`` in order for successive ``urlopen`` calls in github_api.

    Returns the list that accumulates every requested full URL, so tests can
    assert on pagination behaviour without reaching the network.
    """
    requested: list[str] = []
    remaining = list(pages)

    def _fake_urlopen(req: urllib.request.Request, timeout: float = 0) -> _FakeResponse:
        requested.append(req.full_url)
        if not remaining:
            raise AssertionError(f"unexpected extra request: {req.full_url}")
        return _FakeResponse(json.dumps(remaining.pop(0)).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return requested


def _check_run(name: str, conclusion: str | None) -> dict[str, Any]:
    return {"id": 1, "name": name, "status": "completed", "conclusion": conclusion}


def _check_runs_payload(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The REAL GitHub ``/commits/{sha}/check-runs`` response shape: an OBJECT."""
    return {"total_count": len(runs), "check_runs": runs}


# --------------------------------------------------------------------------- #
# Defect 1 — object-shaped check-runs response                                #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_preflight_eligible_true_against_real_object_shaped_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED before the fix: the real endpoint shape is an object, not an array.

    Pre-fix, ``rest_json_array`` raises ``GitHubApiError('unexpected JSON
    response type ...')`` on this payload and the caller swallows it to False,
    so ``occ_preflight_eligible`` can never be True and no streak can ever
    accumulate. This asserts the successful conclusion is actually read.
    """
    _install_transport(
        monkeypatch,
        [
            _check_runs_payload(
                [
                    _check_run("some-other-check", "failure"),
                    _check_run(_PREFLIGHT_CHECK, "success"),
                ]
            )
        ],
    )

    handler = HandlerOccAttestationObserve()
    assert handler._read_occ_preflight_eligible(_REPO, _HEAD_SHA, "token") is True


@pytest.mark.integration
def test_preflight_eligible_false_when_check_concluded_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real failure must read as False for the RIGHT reason (parsed, not raised)."""
    _install_transport(
        monkeypatch,
        [_check_runs_payload([_check_run(_PREFLIGHT_CHECK, "failure")])],
    )

    handler = HandlerOccAttestationObserve()
    assert handler._read_occ_preflight_eligible(_REPO, _HEAD_SHA, "token") is False


@pytest.mark.integration
def test_preflight_eligible_false_when_check_never_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent check-run → not eligible (fail-safe), still via a parsed response."""
    _install_transport(
        monkeypatch,
        [_check_runs_payload([_check_run("unrelated", "success")])],
    )

    handler = HandlerOccAttestationObserve()
    assert handler._read_occ_preflight_eligible(_REPO, _HEAD_SHA, "token") is False


@pytest.mark.integration
def test_preflight_eligible_paginates_over_object_shaped_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pagination must key off the nested ``check_runs`` list length.

    OMN-14442 is the sibling defect: an unpaginated check-runs query goes GREEN
    BY TRUNCATION when the interesting run sorts past page 1. Here the preflight
    check lives on page 2, so a reader that stops after one page reports False.
    """
    page_one = _check_runs_payload(
        [_check_run(f"filler-{i}", "success") for i in range(100)]
    )
    page_two = _check_runs_payload([_check_run(_PREFLIGHT_CHECK, "success")])
    requested = _install_transport(monkeypatch, [page_one, page_two])

    handler = HandlerOccAttestationObserve()
    assert handler._read_occ_preflight_eligible(_REPO, _HEAD_SHA, "token") is True
    assert len(requested) == 2
    assert "page=1" in requested[0]
    assert "page=2" in requested[1]


@pytest.mark.integration
def test_preflight_eligible_uses_newest_run_for_a_rerun_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub returns one entry per rerun; the last one is the current state."""
    _install_transport(
        monkeypatch,
        [
            _check_runs_payload(
                [
                    _check_run(_PREFLIGHT_CHECK, "failure"),
                    _check_run(_PREFLIGHT_CHECK, "success"),
                ]
            )
        ],
    )

    handler = HandlerOccAttestationObserve()
    assert handler._read_occ_preflight_eligible(_REPO, _HEAD_SHA, "token") is True


@pytest.mark.integration
def test_preflight_eligible_false_on_http_error_is_fail_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure stays fail-safe (report-only gate never raises)."""

    def _raise(req: urllib.request.Request, timeout: float = 0) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            404,
            "Not Found",
            {},
            io.BytesIO(b"missing"),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    handler = HandlerOccAttestationObserve()
    assert handler._read_occ_preflight_eligible(_REPO, _HEAD_SHA, "token") is False


# --------------------------------------------------------------------------- #
# Defect 2 — absent observation store must ERROR, not read as zero            #
# --------------------------------------------------------------------------- #


def _observation(
    pr: int, *, clean: bool, observed_at: str
) -> ModelOccAutoauthorObservation:
    return ModelOccAutoauthorObservation(
        product_repo=_REPO,
        product_pr_number=pr,
        occ_pr_number=4000 + pr,
        minted_by_node=clean,
        attestation_match=clean,
        occ_preflight_eligible=clean,
        observed_at=observed_at,
        reason="" if clean else "synthetic non-clean observation",
    )


def _record(pr: int, *, clean: bool, minute: int) -> ModelOccObservationRecord:
    stamp = f"2026-07-21T10:{minute:02d}:00+00:00"
    return ModelOccObservationRecord(
        product_repo=_REPO,
        product_pr_number=pr,
        head_sha=f"{pr:040d}",
        policy_version="v1",
        workflow_run_id=900000 + pr,
        run_attempt=1,
        recorded_at=stamp,
        observation=_observation(pr, clean=clean, observed_at=stamp),
    )


def _write_store(root: Path, records: list[ModelOccObservationRecord]) -> None:
    for record in records:
        path = root / occ_observation_record_relpath(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_occ_observation_record(record))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_observation_store_errors_instead_of_reading_zero(
    tmp_path: Path,
) -> None:
    """An ABSENT ``drift/occ_observations/`` must fail closed.

    Verified live 2026-07-21: that tree does not exist on
    ``onex_change_control@main``, so before this fix every read of the durable
    store reported a clean ``raw_record_count=0`` — indistinguishable from a
    genuinely empty store or a mis-pointed checkout.
    """
    handler = HandlerOccObservationSourceEffect()
    request = ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))

    with pytest.raises(FileNotFoundError) as excinfo:
        await handler.handle(request)

    assert OCC_OBSERVATIONS_ROOT in str(excinfo.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_present_but_empty_observation_store_reads_zero(tmp_path: Path) -> None:
    """A genuinely EMPTY (but present) store is a distinct, valid, zero result."""
    (tmp_path / OCC_OBSERVATIONS_ROOT).mkdir(parents=True)

    result = await HandlerOccObservationSourceEffect().handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )

    assert result.raw_record_count == 0
    assert result.observations == ()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_missing_checkout_dir_errors(tmp_path: Path) -> None:
    """A mis-pointed checkout is a hard error, never a silent zero."""
    with pytest.raises(FileNotFoundError):
        await HandlerOccObservationSourceEffect().handle(
            ModelOccObservationSourceEffectRequest(
                checkout_dir=str(tmp_path / "does-not-exist")
            )
        )


# --------------------------------------------------------------------------- #
# Defect 3 — the composition must emit a CONCRETE streak                      #
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_to_window_composition_emits_concrete_streak(
    tmp_path: Path,
) -> None:
    """source EFFECT → window COMPUTE over a real on-disk store yields N=10.

    This is the composition no workflow performs today (the three nodes are
    referenced by no workflow). Ten distinct source tuples, all clean, must
    produce ``consecutive_clean == 10`` and ``flip_ready is True``.
    """
    records = [_record(1700 + i, clean=True, minute=i) for i in range(10)]
    _write_store(tmp_path, records)

    source = await HandlerOccObservationSourceEffect().handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )
    assert source.raw_record_count == 10
    assert source.distinct_source_tuples == 10

    window = await HandlerOccAutoauthorWindow().handle(
        ModelOccAutoauthorWindowRequest(
            observations=source.observations, required_streak=10
        )
    )

    assert window.total_observations == 10
    assert window.consecutive_clean == 10
    assert window.flip_ready is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_non_clean_observation_resets_the_streak(tmp_path: Path) -> None:
    """Fail-reset is real: a non-clean observation mid-trail resets the count."""
    records = [_record(1700 + i, clean=(i != 5), minute=i) for i in range(10)]
    _write_store(tmp_path, records)

    source = await HandlerOccObservationSourceEffect().handle(
        ModelOccObservationSourceEffectRequest(checkout_dir=str(tmp_path))
    )
    window = await HandlerOccAutoauthorWindow().handle(
        ModelOccAutoauthorWindowRequest(
            observations=source.observations, required_streak=10
        )
    )

    assert window.total_observations == 10
    assert window.consecutive_clean == 4
    assert window.flip_ready is False
    assert window.streak_broken_by == f"{_REPO}#1705"
