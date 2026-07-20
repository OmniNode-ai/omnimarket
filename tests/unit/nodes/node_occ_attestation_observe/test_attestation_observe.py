# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the report-only OCC attestation-observe node (OMN-14393).

Covers the pure resolution helpers, the fail-soft handler contract (a report-only
gate must never raise), and a faithful OFFLINE attestation proof: a byte-matching
on-PR companion is ACCEPTED and a tampered one is REJECTED (so the gate is not
advisory-theater), all without any network.
"""

from __future__ import annotations

import pytest

from omnimarket.events.occ_autoauthor import (
    OCC_MACHINE_MINTED_LABEL,
    ModelOccAutoauthorObservation,
    is_machine_minted,
)
from omnimarket.events.occ_companion import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_attestation_observe.handlers.handler_occ_attestation_observe import (
    HandlerOccAttestationObserve,
    build_observed_files,
    extract_check_conclusion,
    resolve_evidence_source_occ_pr,
    strip_evidence_source_stamp,
)
from omnimarket.nodes.node_occ_attestation_observe.models.model_occ_attestation_observe_request import (
    ModelOccAttestationObserveRequest,
)
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)

_OCC_HEAD_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f001234567"


# --------------------------------------------------------------------------- #
# Pure helpers                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_is_machine_minted() -> None:
    assert is_machine_minted([OCC_MACHINE_MINTED_LABEL, "other"]) is True
    assert is_machine_minted(["other"]) is False
    assert is_machine_minted([]) is False


@pytest.mark.unit
def test_resolve_evidence_source_occ_pr() -> None:
    assert (
        resolve_evidence_source_occ_pr("Closes OMN-1\nEvidence-Source: OCC#4284")
        == 4284
    )
    assert resolve_evidence_source_occ_pr("Closes OMN-1") is None


@pytest.mark.unit
def test_strip_evidence_source_stamp_is_idempotent_and_ticket_preserving() -> None:
    body = "Closes OMN-14608\n\nEvidence-Source: OCC#4284"
    stripped = strip_evidence_source_stamp(body)
    assert "Evidence-Source" not in stripped
    assert "OMN-14608" in stripped
    # No stamp → returned unchanged.
    assert strip_evidence_source_stamp(stripped) == stripped


@pytest.mark.unit
def test_extract_check_conclusion_returns_newest() -> None:
    runs: list[dict[str, object]] = [
        {"name": "occ-preflight / eligibility", "conclusion": "failure"},
        {"name": "other", "conclusion": "success"},
        {"name": "occ-preflight / eligibility", "conclusion": "success"},
    ]
    assert extract_check_conclusion(runs, "occ-preflight / eligibility") == "success"
    assert extract_check_conclusion(runs, "missing") is None


@pytest.mark.unit
def test_build_observed_files_drops_missing() -> None:
    plan = _plan_for(_request_with_stamp())
    files = plan.companion_files
    assert files
    # Present-for-all → same count.
    full = build_observed_files(files, {f.path: f.content for f in files})
    assert len(full) == len(files)
    # Missing one path → dropped (shrinks the set → will fail the fingerprint).
    partial_map = {f.path: f.content for f in files[1:]}
    partial = build_observed_files(files, partial_map)
    assert len(partial) == len(files) - 1


# --------------------------------------------------------------------------- #
# Handler — offline (stubbed I/O)                                             #
# --------------------------------------------------------------------------- #


def _request_with_stamp() -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=1760,
        pr_head_sha="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        pr_title="feat(OMN-14608): thing",
        pr_body="Closes OMN-14608\n\nEvidence-Source: OCC#4284",
        run_timestamp="2026-07-14T12:00:00Z",
        product_probe=ModelObservedProbe(
            command="gh pr view 1760",
            stdout='{"number":1760,"state":"OPEN"}',
            exit_code=0,
        ),
    )


def _plan_for(req: ModelOccCompanionRequest):  # type: ignore[no-untyped-def]
    """Recompute the pass-2 plan the handler will attest against (identical inputs)."""
    v2 = req.model_copy(
        update={
            "pr_body": strip_evidence_source_stamp(req.pr_body),
            "occ_pr_number": 4284,
            "occ_head_sha": _OCC_HEAD_SHA,
            "occ_probe": ModelObservedProbe(
                command="gh pr view 4284 --repo OmniNode-ai/onex_change_control --json number,state",
                stdout='{"number":4284,"state":"OPEN"}',
                exit_code=0,
            ),
        }
    )
    return compute_companion_plan(v2)


class _StubState(HandlerOccStateEffect):
    def __init__(self, req: ModelOccCompanionRequest) -> None:
        self._req = req

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        return self._req


class _OfflineObserve(HandlerOccAttestationObserve):
    """Overrides every network boundary with in-memory content."""

    def __init__(
        self,
        req: ModelOccCompanionRequest,
        content_by_path: dict[str, str],
        *,
        minted: bool,
        eligible: bool,
    ) -> None:
        super().__init__(state_handler=_StubState(req))
        self._content = content_by_path
        self._minted = minted
        self._eligible = eligible

    def _resolve_github_token(self) -> str:
        return "token"

    def _read_occ_preflight_eligible(
        self, repo: str, head_sha: str, token: str
    ) -> bool:
        return self._eligible

    def _read_occ_pr_head_and_marker(
        self, occ_repo: str, occ_pr_number: int, token: str
    ) -> tuple[str, bool]:
        return _OCC_HEAD_SHA, self._minted

    def _content_at_ref(self, repo: str, path: str, ref: str, token: str) -> str | None:
        return self._content.get(path)


def _observe_request() -> ModelOccAttestationObserveRequest:
    return ModelOccAttestationObserveRequest(
        repo="OmniNode-ai/omnimarket", pr_number=1760
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_byte_matching_machine_minted_companion_is_clean() -> None:
    req = _request_with_stamp()
    plan = _plan_for(req)
    content = {f.path: f.content for f in plan.companion_files}
    handler = _OfflineObserve(req, content, minted=True, eligible=True)

    obs = await handler.handle(_observe_request())

    assert obs.occ_pr_number == 4284
    assert obs.minted_by_node is True
    assert obs.attestation_match is True
    assert obs.occ_preflight_eligible is True
    assert obs.is_clean is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tampered_companion_is_rejected() -> None:
    req = _request_with_stamp()
    plan = _plan_for(req)
    # Tamper: append a byte to every file's content → not byte-reproducible.
    content = {f.path: f.content + "\n# tampered\n" for f in plan.companion_files}
    handler = _OfflineObserve(req, content, minted=True, eligible=True)

    obs = await handler.handle(_observe_request())

    assert obs.attestation_match is False
    assert obs.is_clean is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_companion_file_is_rejected() -> None:
    req = _request_with_stamp()
    plan = _plan_for(req)
    files = plan.companion_files
    # Drop one file entirely → observed set shrinks → fingerprint mismatch.
    content = {f.path: f.content for f in files[1:]}
    handler = _OfflineObserve(req, content, minted=True, eligible=True)

    obs = await handler.handle(_observe_request())
    assert obs.attestation_match is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_minted_but_matching_is_not_clean() -> None:
    # A byte-reproducible companion that is NOT machine-minted (no marker label)
    # is not flip-eligible — is_clean requires minted_by_node.
    req = _request_with_stamp()
    plan = _plan_for(req)
    content = {f.path: f.content for f in plan.companion_files}
    handler = _OfflineObserve(req, content, minted=False, eligible=True)

    obs = await handler.handle(_observe_request())
    assert obs.attestation_match is True
    assert obs.minted_by_node is False
    assert obs.is_clean is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unstamped_product_pr_yields_not_clean_observation() -> None:
    req = _request_with_stamp().model_copy(update={"pr_body": "Closes OMN-14608"})
    handler = _OfflineObserve(req, {}, minted=True, eligible=True)

    obs = await handler.handle(_observe_request())
    assert obs.occ_pr_number is None
    assert obs.attestation_match is False
    assert obs.is_clean is False
    assert "Evidence-Source" in obs.reason


class _RealTokenObserve(HandlerOccAttestationObserve):
    """Uses the REAL ``_resolve_github_token()`` (not stubbed) to exercise the
    sync-in-async regression directly; every other network boundary is
    stubbed offline, exactly like ``_OfflineObserve``."""

    def __init__(
        self,
        req: ModelOccCompanionRequest,
        content_by_path: dict[str, str],
        *,
        minted: bool,
        eligible: bool,
    ) -> None:
        super().__init__(state_handler=_StubState(req))
        self._content = content_by_path
        self._minted = minted
        self._eligible = eligible

    def _read_occ_preflight_eligible(
        self, repo: str, head_sha: str, token: str
    ) -> bool:
        return self._eligible

    def _read_occ_pr_head_and_marker(
        self, occ_repo: str, occ_pr_number: int, token: str
    ) -> tuple[str, bool]:
        return _OCC_HEAD_SHA, self._minted

    def _content_at_ref(self, repo: str, path: str, ref: str, token: str) -> str | None:
        return self._content.get(path)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_github_token_is_awaited_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMN-14844 regression: ``_resolve_github_token()`` calls ``resolve_api_key()``,
    which RAISES if invoked from inside a running event loop. ``_observe`` is
    ``async def`` and pytest-asyncio always runs it inside a running loop, so
    the REAL (unstubbed) token resolution must be called off the loop via
    ``asyncio.to_thread(...)`` -- exactly like its 3 sibling I/O calls three
    lines below it -- or every real dispatch fail-softs: occ_pr_number stays
    None and every attestation boolean stays False, making the OMN-13976 N=10
    real-doneness gate permanently unsatisfiable.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "fake-token-for-omn-14844-regression")
    req = _request_with_stamp()
    plan = _plan_for(req)
    content = {f.path: f.content for f in plan.companion_files}
    handler = _RealTokenObserve(req, content, minted=True, eligible=True)

    obs = await handler.handle(_observe_request())

    assert "RuntimeError" not in (obs.reason or "")
    assert obs.occ_pr_number == 4284
    assert obs.attestation_match is True
    assert obs.is_clean is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_handler_is_fail_soft_on_error() -> None:
    # A read boundary that raises must NOT propagate — report-only gate emits a
    # typed not-clean observation instead.
    class _Boom(HandlerOccAttestationObserve):
        def _resolve_github_token(self) -> str:
            raise RuntimeError("secret store down")

    obs = await _Boom().handle(_observe_request())
    assert isinstance(obs, ModelOccAutoauthorObservation)
    assert obs.is_clean is False
    assert "error" in obs.reason.lower()
