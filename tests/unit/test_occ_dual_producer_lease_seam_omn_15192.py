# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15192: cross-boundary regression tests for the SHARED OCC lease seam.

Both OCC companion producers contend on **one** ref namespace in
``onex_change_control``:

* ``OccCompanionEmitter`` (``node_pr_lifecycle_fix_effect``) — the legacy leg,
  today the only routinely CI-wired, automatically-firing producer;
* ``HandlerOccCompanionEffect`` (``node_occ_companion_effect``) — the canonical
  leg, wired only behind ``OMNI_OCC_AUTOAUTHOR_MODE=mutate`` (UNSET org-wide, so
  it defaults to ``dry_run`` everywhere today).

OMN-15192 flips that variable and disables the legacy dispatch **in one atomic
change**. During any window where both legs are live, the single-producer lease
(OMN-14783/14793/14941) is the *only* thing preventing two producers from
force-pushing competing companions onto the same deterministic ``auto/*``
branch. That makes the lease a genuine cross-boundary seam.

Until now it was covered by two INDEPENDENT unit suites — the emitter's lease
behaviour in ``test_occ_companion_emitter*.py`` and the effect's in
``test_lease_single_producer_omn_14941.py`` — each of which stubs
``acquire_occ_companion_lease`` inside its own module. Neither proves the legs
agree, which is exactly the failure mode CLAUDE.md "Define and match seams"
names: two individually-green suites over a seam nobody drives. A one-sided
change to either leg's key inputs or TTL would leave both suites green while
silently re-enabling dual authoring in production.

These tests drive **both real producers against one shared in-memory lease
surface**, through the REAL ``occ_git_transport`` acquire/release functions
(never stubbed here). The transport's own semantics — the ``201``/``422``
create-if-absent contract and the ``<owner>-<repo>-pr-<n>-<head>`` key shape —
are treated as FROZEN SEAMS and are asserted, not modified.

Seam contract asserted here (field by field):

===================  ==========================================  ==========================================
Lease seam field     ``HandlerOccCompanionEffect``               ``OccCompanionEmitter``
===================  ==========================================  ==========================================
``repo_slug``        ``request.repo``                            ``repo_slug``
``pr_number``        ``request.pr_number``                       ``pr_number``
``head_sha``         product head from the RSD-2 state read      product head from ``GET /pulls/{n}``
``occ_repo``         ``request.occ_repo``                        ``self._occ_repo``
``lease_ttl_seconds````_LEASE_TTL_SECONDS`` (900)                ``_DEFAULT_LEASE_TTL_SECONDS`` (900)
``producer_id``      ``node_occ_companion_effect@<host>``        ``<runner>@<host>`` — INTENTIONALLY differs
===================  ==========================================  ==========================================

Every field except ``producer_id`` must agree, because ``_lease_key`` is
``(repo_slug, pr_number, head_sha)`` and the stale-steal decision is
``lease_ttl_seconds``. ``producer_id`` is deliberately per-producer: it is
written into the lease commit body for forensics and is NOT part of the key.

RED-vs-EXISTS-but-WRONG: each test here fails against a repo where either leg's
seam inputs drift (a differing key normalisation, a differing TTL, a leg that
stops taking the lease before mutating). It does not merely assert that a
function exists.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import pytest

from omnimarket import occ_git_transport as transport
from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_occ_companion_effect.handlers import (
    handler_occ_companion_effect as effect_mod,
)
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    HandlerOccCompanionEffect,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)
from omnimarket.nodes.node_occ_state_effect.handlers.handler_occ_state_effect import (
    HandlerOccStateEffect,
)
from omnimarket.nodes.node_occ_state_effect.models.model_occ_state_request import (
    ModelOccStateRequest,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter import (
    OccCompanionEmitter,
)

_EMITTER_MOD = (
    "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_emitter"
)

# The ONE product PR head both legs contend on. Both producers observe the
# identical ``head.sha`` from GitHub, which is why it is the key discriminator.
_REPO = "OmniNode-ai/omnimarket"
_PR = 1760
_HEAD_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
_OTHER_HEAD_SHA = "f" * 40
_OCC_REPO = "OmniNode-ai/onex_change_control"

# The lease key shape is a FROZEN SEAM (``transport._lease_key``): normalised
# ``<owner>-<repo>-pr-<n>-<head>``, slashes to dashes, lower-cased. Spelled out
# literally here so a change to the normalisation fails this file loudly rather
# than silently re-partitioning the namespace between the two legs.
_EXPECTED_REF = f"refs/occ-companion-leases/omninode-ai-omnimarket-pr-{_PR}-{_HEAD_SHA}"


@pytest.fixture(autouse=True)
def _pin_legacy_check_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``OMNI_OCC_CHECK_BINDING=pr_existence`` for this module.

    Same rationale as ``test_occ_companion_emitter.py``: the emitter driver here
    does not inject a RED-derivable diff, so under the ``content_bound`` default
    it would take the fail-closed ``skip:NO_RED_DERIVABLE_CHECK`` branch and
    return BEFORE reaching the lease. The lease seam is binding-orthogonal, so
    pinning the legacy binding keeps these tests pointed at the seam they are
    written for.
    """
    monkeypatch.setenv("OMNI_OCC_CHECK_BINDING", "pr_existence")


# ---------------------------------------------------------------------------
# The shared lease surface — ONE store, both legs
# ---------------------------------------------------------------------------


class _SharedLeaseSurface:
    """In-memory stand-in for the OCC repo's git-refs API.

    Implements exactly the endpoints ``occ_git_transport`` uses, with GitHub's
    real status semantics for the seam that matters: ``POST /git/refs`` is
    create-if-absent (201 create / **422** when the ref exists), which is the
    atomicity the whole single-producer protocol rests on.

    Both producers are pointed at ONE instance, so a lease taken by either leg
    is visible to the other — the cross-boundary property under test.
    """

    def __init__(self) -> None:
        self.refs: dict[str, str] = {}
        # ref name -> committer date (ISO8601) of its lease commit
        self.commit_dates: dict[str, str] = {}
        self.commits: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.created: list[str] = []
        self._next_sha = 0

    @staticmethod
    def _server_now() -> str:
        """Server-stamped commit time, as GitHub does on ``POST /git/commits``.

        This must be *real* now, not a frozen literal: ``_lease_is_stale``
        compares ``committer.date`` against wall-clock now, so a fixed past
        timestamp would make every freshly minted lease instantly TTL-stealable
        and quietly turn the contention tests into steal tests.
        """
        return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")

    # -- helpers ---------------------------------------------------------
    def _mint_sha(self) -> str:
        self._next_sha += 1
        return f"{self._next_sha:040x}"

    def seed_lease(self, ref: str, *, committed_at: str) -> None:
        """Plant a pre-existing lease ref (used for the orphan/stale fixtures)."""
        sha = self._mint_sha()
        self.refs[ref] = sha
        self.commits[sha] = {"committer": {"date": committed_at}}

    def lease_refs(self) -> list[str]:
        return sorted(self.refs)

    # -- transport surface ------------------------------------------------
    def rest_json(
        self, method: str, path: str, *, token: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        parts = path.strip("/").split("/")
        if method == "GET" and parts[:1] == ["repos"] and len(parts) == 3:
            return {"default_branch": "main"}  # GET /repos/{owner}/{repo}
        if method == "GET" and path.endswith("/commits/main"):
            return {"commit": {"tree": {"sha": "tree" + "0" * 36}}}
        if method == "POST" and path.endswith("/git/commits"):
            sha = self._mint_sha()
            assert body is not None
            self.commits[sha] = {"committer": {"date": self._server_now()}, **body}
            return {"sha": sha}
        if method == "POST" and path.endswith("/git/refs"):
            assert body is not None
            ref = str(body["ref"])
            if ref in self.refs:
                # GitHub's create-if-absent contract: 422 "Reference already
                # exists". This 422 is a SEMANTIC outcome of the lease protocol,
                # never a transport hiccup — asserted as a frozen seam below.
                raise GitHubApiError("Reference already exists", status_code=422)
            self.refs[ref] = str(body["sha"])
            self.created.append(ref)
            return {"ref": ref}
        if method == "GET" and "/git/ref/" in path:
            ref = "refs/" + path.split("/git/ref/", 1)[1]
            if ref not in self.refs:
                raise GitHubApiError("Not Found", status_code=404)
            return {"object": {"sha": self.refs[ref]}}
        if method == "GET" and "/git/commits/" in path:
            sha = path.rsplit("/", 1)[1]
            if sha not in self.commits:
                raise GitHubApiError("Not Found", status_code=404)
            return self.commits[sha]
        raise AssertionError(f"unexpected lease-surface call: {method} {path}")

    def rest_json_array(
        self, method: str, path: str, *, token: str
    ) -> list[dict[str, Any]]:
        if method == "GET" and "/git/matching-refs/" in path:
            prefix = "refs/" + path.split("/git/matching-refs/", 1)[1]
            return [
                {"ref": r, "object": {"sha": s}}
                for r, s in sorted(self.refs.items())
                if r.startswith(prefix)
            ]
        raise AssertionError(f"unexpected lease-surface array call: {method} {path}")

    def rest_no_content(self, method: str, path: str, *, token: str) -> None:
        if method == "DELETE" and "/git/refs/" in path:
            ref = "refs/" + path.split("/git/refs/", 1)[1]
            if ref not in self.refs:
                raise GitHubApiError("Not Found", status_code=404)
            del self.refs[ref]
            self.deleted.append(ref)
            return
        raise AssertionError(f"unexpected lease-surface delete: {method} {path}")


@pytest.fixture
def surface(monkeypatch: pytest.MonkeyPatch) -> _SharedLeaseSurface:
    """Point the REAL transport at one shared store for the whole test.

    ``acquire_occ_companion_lease`` / ``release_occ_companion_lease`` themselves
    are NEVER patched in this module — only the HTTP surface underneath them —
    so both legs execute the real lease protocol.
    """
    store = _SharedLeaseSurface()
    monkeypatch.setattr(transport, "rest_json", store.rest_json)
    monkeypatch.setattr(transport, "rest_json_array", store.rest_json_array)
    monkeypatch.setattr(transport, "rest_no_content", store.rest_no_content)
    return store


# ---------------------------------------------------------------------------
# Leg drivers — both REAL producers, no stubbed acquire/release
# ---------------------------------------------------------------------------


class _StubStateHandler(HandlerOccStateEffect):
    def __init__(self, request: ModelOccCompanionRequest) -> None:
        self._request = request

    async def handle(self, request: ModelOccStateRequest) -> ModelOccCompanionRequest:
        return self._request


def _canned_companion_request(head_sha: str = _HEAD_SHA) -> ModelOccCompanionRequest:
    return ModelOccCompanionRequest(
        repo=_REPO,
        pr_number=_PR,
        pr_head_sha=head_sha,
        pr_title="feat(OMN-15192): thing",
        pr_body="Closes OMN-15192",
        run_timestamp="2026-07-29T00:00:00Z",
        product_probe=ModelObservedProbe(
            command=f"gh pr view {_PR}",
            stdout=json.dumps({"number": _PR, "state": "OPEN"}),
            exit_code=0,
        ),
    )


class _MintTripwireError(RuntimeError):
    """Raised the instant a leg tries to mutate after winning the lease."""


async def drive_effect_leg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head_sha: str = _HEAD_SHA,
    suppress_release: bool = False,
) -> str:
    """Run the REAL canonical leg (``HandlerOccCompanionEffect``) to the lease.

    Its own mint I/O is tripwired: if the leg wins the lease it will raise
    ``_MintTripwireError`` at the first git/REST call, which is caught here. That
    still exercises the real acquire and, unless ``suppress_release``, the real
    ``finally`` release.

    ``suppress_release=True`` models a producer that is **still mid-mint** and
    therefore still holding the lease — the exact live condition the other leg
    must detect. Only the release is suppressed; the acquire is real.
    """

    def _boom(*_a: object, **_k: object) -> object:
        raise _MintTripwireError("effect leg mutated after taking the lease")

    monkeypatch.setattr(effect_mod, "_resolve_github_token", lambda: "t")
    monkeypatch.setattr(effect_mod, "run_git", _boom)
    monkeypatch.setattr(effect_mod, "rest_json", _boom)
    monkeypatch.setattr(effect_mod, "rest_json_array", _boom)
    if suppress_release:
        monkeypatch.setattr(
            effect_mod, "release_occ_companion_lease", lambda **_k: None
        )

    handler = HandlerOccCompanionEffect(
        state_handler=_StubStateHandler(_canned_companion_request(head_sha))
    )
    try:
        result = await handler.handle(
            ModelOccCompanionEffectRequest(repo=_REPO, pr_number=_PR, mode="mutate")
        )
    except _MintTripwireError:
        return "won-lease:mint-tripwired"
    return str(result.action)


class _FakeTempDir:
    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> Literal[False]:
        return False


def drive_emitter_leg(
    tmp_path: Path,
    *,
    head_sha: str = _HEAD_SHA,
    suppress_release: bool = False,
) -> str:
    """Run the REAL legacy leg (``OccCompanionEmitter``) to the lease.

    Everything upstream of the lease (product-PR read, contract render) is
    mocked exactly as ``test_occ_companion_emitter.TestFullEmitFlow`` does;
    everything downstream is a tripwire. ``acquire_occ_companion_lease`` is NOT
    patched — that is the seam under test.
    """
    emitter = OccCompanionEmitter()

    def fake_rest(
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        if path.endswith(f"/pulls/{_PR}"):
            return {
                "body": "Implements the thing.",
                "title": "feat(OMN-15192): thing",
                "head": {"sha": head_sha, "ref": "feature-branch"},
                "state": "open",
            }
        return {}

    def _boom(*_a: object, **_k: object) -> object:
        raise _MintTripwireError("emitter leg mutated after taking the lease")

    ctxs = [
        patch(f"{_EMITTER_MOD}.rest_json", side_effect=fake_rest),
        patch(f"{_EMITTER_MOD}._resolve_github_token", return_value="t"),
        patch.object(emitter, "_run_git", side_effect=_boom),
        patch.object(emitter, "_clone_and_branch", side_effect=_boom),
        patch(
            f"{_EMITTER_MOD}.tempfile.TemporaryDirectory",
            return_value=_FakeTempDir(tmp_path),
        ),
    ]
    if suppress_release:
        ctxs.append(patch(f"{_EMITTER_MOD}.release_occ_companion_lease"))

    from contextlib import ExitStack

    with ExitStack() as stack:
        for c in ctxs:
            stack.enter_context(c)
        try:
            return emitter._emit_companion_sync(_REPO, _PR, None)
        except _MintTripwireError:
            return "won-lease:mint-tripwired"


# ---------------------------------------------------------------------------
# 1. Static seam parity — the constants that must not drift apart
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLeaseSeamConstantParity:
    def test_both_legs_declare_the_same_lease_ttl(self) -> None:
        """A TTL split silently breaks stale-steal symmetry across the legs.

        If the effect leg's TTL were shorter than the emitter's, the effect leg
        would consider a *live* emitter mint "stale" and steal its lease —
        re-creating the dual-producer race the lease exists to close, with both
        per-leg suites still green.
        """
        from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
            occ_companion_emitter as emitter_mod,
        )

        assert (
            effect_mod._LEASE_TTL_SECONDS == emitter_mod._DEFAULT_LEASE_TTL_SECONDS
        ), (
            "the two OCC producers contend on the SAME lease refs, so their TTL "
            "semantics must match exactly (OMN-14793 / OMN-15192)"
        )

    def test_lease_key_shape_is_the_frozen_normalised_form(self) -> None:
        """Key shape is a FROZEN SEAM — both legs must hash to the same string.

        ``_lease_key`` normalises ``owner/repo`` and ``owner-repo`` to one form,
        so a leg passing either spelling still lands on the same ref.
        """
        slashed = transport._lease_key(_REPO, _PR, _HEAD_SHA)
        dashed = transport._lease_key("OmniNode-ai-omnimarket", _PR, _HEAD_SHA)
        assert slashed == dashed == f"omninode-ai-omnimarket-pr-{_PR}-{_HEAD_SHA}"
        assert f"refs/occ-companion-leases/{slashed}" == _EXPECTED_REF


# ---------------------------------------------------------------------------
# 2. The cross-boundary race — both real legs, one shared surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDualProducerContendOnOneLease:
    @pytest.mark.asyncio
    async def test_emitter_holds_then_effect_leg_skips_with_zero_side_effects(
        self,
        surface: _SharedLeaseSurface,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """The OMN-15192 flip window, driven for real: emitter first, effect second."""
        emitter_action = drive_emitter_leg(tmp_path, suppress_release=True)
        assert emitter_action == "won-lease:mint-tripwired"
        assert surface.lease_refs() == [_EXPECTED_REF]

        effect_action = await drive_effect_leg(monkeypatch)

        # The effect leg must observe the emitter's lease and no-op. Its mint
        # tripwires would have raised (surfacing as "won-lease:...") had it
        # proceeded, so this assertion IS the zero-side-effects proof.
        assert "LEASE_HELD" in effect_action
        assert surface.lease_refs() == [_EXPECTED_REF]
        # It must NOT release a lease it never acquired — that would yank the
        # live producer's lock out from under it.
        assert surface.deleted == []

    @pytest.mark.asyncio
    async def test_effect_leg_holds_then_emitter_skips_with_zero_side_effects(
        self,
        surface: _SharedLeaseSurface,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Symmetric direction — neither leg is privileged over the other."""
        effect_action = await drive_effect_leg(monkeypatch, suppress_release=True)
        assert effect_action == "won-lease:mint-tripwired"
        assert surface.lease_refs() == [_EXPECTED_REF]

        emitter_action = drive_emitter_leg(tmp_path)

        assert "LEASE_HELD" in emitter_action
        assert surface.lease_refs() == [_EXPECTED_REF]
        assert surface.deleted == []

    @pytest.mark.asyncio
    async def test_both_legs_target_the_byte_identical_ref_for_one_pr_head(
        self,
        surface: _SharedLeaseSurface,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Key identity: the whole protocol collapses if the legs disagree here.

        Each leg is run in isolation against a fresh view of the namespace and
        must create the SAME ref name. If either leg's ``repo_slug`` /
        ``pr_number`` / ``head_sha`` wiring drifts, the two legs partition into
        disjoint keys, both "win", and both mint — dual authoring with two green
        per-leg suites.
        """
        await drive_effect_leg(monkeypatch)  # acquires and releases in finally
        effect_created = list(surface.created)

        drive_emitter_leg(tmp_path)
        emitter_created = surface.created[len(effect_created) :]

        assert effect_created == [_EXPECTED_REF]
        assert emitter_created == [_EXPECTED_REF]

    @pytest.mark.asyncio
    async def test_release_by_one_leg_hands_the_head_to_the_other(
        self,
        surface: _SharedLeaseSurface,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Hand-off: the effect leg's ``finally`` release frees the head for the emitter.

        This is the non-degenerate direction — a release that did not actually
        reach the shared surface would leave the second leg locked out until the
        900s TTL, which is a liveness bug the per-leg suites cannot see.
        """
        await drive_effect_leg(monkeypatch)  # real acquire + real finally release
        assert surface.lease_refs() == [], (
            "effect leg must free the head on mint failure"
        )
        assert surface.deleted == [_EXPECTED_REF]

        emitter_action = drive_emitter_leg(tmp_path, suppress_release=True)
        assert emitter_action == "won-lease:mint-tripwired"
        assert surface.lease_refs() == [_EXPECTED_REF]


# ---------------------------------------------------------------------------
# 3. Frozen transport semantics the seam depends on (201 / 422)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAcquireStatusSemanticsAreFrozen:
    def test_201_creates_and_422_is_contention_not_a_transport_error(
        self, surface: _SharedLeaseSurface
    ) -> None:
        """``201`` = won, ``422`` = a live holder. Neither may be retried away.

        OMN-15347 added bounded retry to this module for *transient* shapes. A
        422 is a semantic lease outcome — retrying it would convert "another
        producer holds this" into "I won", which is precisely the dual-producer
        defect. Pinned here from the seam's side, in addition to the retry
        unit tests on the transport's side.
        """
        first = transport.acquire_occ_companion_lease(
            token="t",
            repo_slug=_REPO,
            pr_number=_PR,
            head_sha=_HEAD_SHA,
            producer_id="leg-a@host",
            lease_ttl_seconds=900,
            occ_repo=_OCC_REPO,
        )
        second = transport.acquire_occ_companion_lease(
            token="t",
            repo_slug=_REPO,
            pr_number=_PR,
            head_sha=_HEAD_SHA,
            producer_id="leg-b@host",
            lease_ttl_seconds=900,
            occ_repo=_OCC_REPO,
        )
        assert first is True
        assert second is False
        assert surface.lease_refs() == [_EXPECTED_REF]

    def test_producer_id_is_recorded_but_is_not_part_of_the_key(
        self, surface: _SharedLeaseSurface
    ) -> None:
        """``producer_id`` intentionally differs per leg and must NOT key the ref.

        If it ever entered the key the two legs would occupy disjoint refs and
        never contend — the silent dual-authoring regression.
        """
        transport.acquire_occ_companion_lease(
            token="t",
            repo_slug=_REPO,
            pr_number=_PR,
            head_sha=_HEAD_SHA,
            producer_id="node_occ_companion_effect@host-1",
            lease_ttl_seconds=900,
            occ_repo=_OCC_REPO,
        )
        (ref,) = surface.lease_refs()
        assert ref == _EXPECTED_REF
        body = surface.commits[surface.refs[ref]]["message"]
        assert "node_occ_companion_effect@host-1" in body


# ---------------------------------------------------------------------------
# 4. OMN-15347 reap is leg-agnostic (cross-boundary GC)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOrphanReapCrossesLegs:
    @pytest.mark.asyncio
    async def test_effect_leg_reaps_an_expired_orphan_left_by_the_other_leg(
        self,
        surface: _SharedLeaseSurface,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An orphan is debris regardless of which leg abandoned it.

        OMN-15347's ``_reap_stale_sibling_leases`` keys only on repo + PR +
        head, never on producer identity, so either leg collects either leg's
        stale-head debris. Without that property the flip would need a
        per-producer GC path.
        """
        orphan = (
            f"refs/occ-companion-leases/omninode-ai-omnimarket-pr-{_PR}-"
            f"{_OTHER_HEAD_SHA}"
        )
        # Planted as if the EMITTER leg took it days ago and never released.
        surface.seed_lease(orphan, committed_at="2026-07-25T00:30:14Z")
        assert orphan in surface.lease_refs()

        await drive_effect_leg(monkeypatch)  # acquires at the CURRENT head

        assert orphan in surface.deleted, "expired cross-leg orphan must be reaped"
        assert orphan not in surface.lease_refs()

    @pytest.mark.asyncio
    async def test_an_unexpired_sibling_from_the_other_leg_is_never_reaped(
        self,
        surface: _SharedLeaseSurface,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A live producer's lease on an older head must survive the GC.

        The dangerous over-reach direction: reaping a sibling that a live
        producer still holds would let two legs mint concurrently for the same
        PR on adjacent heads.
        """
        fresh = (
            f"refs/occ-companion-leases/omninode-ai-omnimarket-pr-{_PR}-"
            f"{_OTHER_HEAD_SHA}"
        )
        surface.seed_lease(
            fresh,
            committed_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        )

        await drive_effect_leg(monkeypatch)

        assert fresh not in surface.deleted
        assert fresh in surface.lease_refs()
