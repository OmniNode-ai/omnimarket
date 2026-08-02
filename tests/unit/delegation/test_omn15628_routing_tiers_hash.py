# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first tests for the OMN-15628 ``routing_tiers_hash`` resolution defect.

``HandlerDelegationWorkflow._routing_tiers_hash()`` is the replay-determinism
provenance field stamped onto every terminal ``ModelDelegationResult``: it is
supposed to be the SHA-256 of the ``routing_tiers.yaml`` bytes the routing
authority actually resolved. At the pre-fix head it returned ``None``
unconditionally, because its packaged-default path arithmetic walked
``Path(__file__).parent`` **five** times (``src/configs/routing_tiers.yaml`` —
a path that does not exist in this repo; ``git ls-tree`` shows exactly one
``routing_tiers.yaml``, under ``src/omnimarket/configs/``) and it never read
the ``DELEGATION_ROUTING_TIERS_PATH`` env pin that the routing reducer's
``_get_config()`` treats as the authoritative binding.

Consequence: every replay/provenance consumer of ``routing_tiers_hash`` saw a
null hash, so a routing-config change could not be detected across replays.

These tests drive the SHIPPED ``_routing_tiers_hash()`` code path with a real
filesystem and a real env binding — no resolver mock, no monkeypatched
``Path``, nothing that would let the defect pass
(``feedback_prove_red_against_exists_but_wrong``).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.routing.routing_tiers_path import (
    ROUTING_TIERS_PACKAGED_DEFAULT_PATH,
    resolve_routing_tiers_path,
)

pytestmark = pytest.mark.unit


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestRoutingTiersHashResolvesRealBytes:
    """AC: the hash is the sha256 of the tiers file the routing authority reads."""

    def test_hash_matches_sha256_of_env_pinned_tiers_file(self) -> None:
        """With the canonical binding in place, the hash is that file's sha256.

        ``tests/conftest.py::_ensure_delegation_routing_tiers_path`` binds
        ``DELEGATION_ROUTING_TIERS_PATH`` to the packaged canonical file, which
        is the same binding a deployment's contract overlay supplies. RED at the
        pre-fix head: ``_routing_tiers_hash()`` returned ``None``.
        """
        pinned = Path(os.environ["DELEGATION_ROUTING_TIERS_PATH"])
        assert pinned.exists(), (
            "precondition: the env-pinned tiers file must exist for this test to "
            f"mean anything (bound to {pinned})"
        )

        observed = HandlerDelegationWorkflow._routing_tiers_hash()

        assert observed is not None, (
            "routing_tiers_hash must not be None when DELEGATION_ROUTING_TIERS_PATH "
            f"is bound to an existing file ({pinned})"
        )
        assert observed == _sha256_of(pinned)

    def test_hash_follows_the_env_pin_not_the_packaged_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Re-pinning the env key must change the hash.

        This is the discriminating case: a hash derived from a hardcoded
        packaged path (or a stale ``.parent`` walk) would be unchanged — or
        ``None`` — when the binding moves. Distinct bytes are used so the two
        files cannot collide.
        """
        packaged = Path(os.environ["DELEGATION_ROUTING_TIERS_PATH"])
        packaged_hash = _sha256_of(packaged)

        repinned = tmp_path / "routing_tiers.yaml"
        repinned.write_bytes(
            packaged.read_bytes() + b"\n# OMN-15628 re-pin discriminator\n"
        )
        monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(repinned))

        observed = HandlerDelegationWorkflow._routing_tiers_hash()

        assert observed is not None
        assert observed == _sha256_of(repinned)
        assert observed != packaged_hash, (
            "the hash must track the DELEGATION_ROUTING_TIERS_PATH binding, not a "
            "hardcoded packaged path"
        )

    def test_unbound_env_falls_back_to_the_packaged_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no binding, the hash degrades to the packaged file, not ``None``.

        Provenance recording must not abort a result that has already been
        produced, so — unlike the config loader's rule-8 refusal — this surface
        falls back. The fallback target is the shared
        ``omnimarket.routing.routing_tiers_path.ROUTING_TIERS_PACKAGED_DEFAULT_PATH``
        constant — the same one the routing authority reads — which is what makes
        the ``.parent``-arithmetic drift this ticket fixed impossible to
        reintroduce.
        """
        monkeypatch.delenv("DELEGATION_ROUTING_TIERS_PATH", raising=False)

        observed = HandlerDelegationWorkflow._routing_tiers_hash()

        assert observed is not None
        assert observed == _sha256_of(ROUTING_TIERS_PACKAGED_DEFAULT_PATH)


class TestPackagedDefaultPathArithmetic:
    """The packaged constant must name the one committed tiers file."""

    def test_packaged_default_is_the_single_committed_tiers_file(self) -> None:
        assert ROUTING_TIERS_PACKAGED_DEFAULT_PATH.name == "routing_tiers.yaml"
        assert ROUTING_TIERS_PACKAGED_DEFAULT_PATH.exists(), (
            "the packaged-default arithmetic must resolve to an existing file; "
            "the pre-fix orchestrator copy walked one .parent too far and "
            f"pointed at a nonexistent path ({ROUTING_TIERS_PACKAGED_DEFAULT_PATH})"
        )
        assert ROUTING_TIERS_PACKAGED_DEFAULT_PATH.parent.name == "configs"
        assert ROUTING_TIERS_PACKAGED_DEFAULT_PATH.parent.parent.name == "omnimarket"

    def test_resolver_returns_the_env_pin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pinned = tmp_path / "tiers.yaml"
        pinned.write_text("tiers: []\n")
        monkeypatch.setenv("DELEGATION_ROUTING_TIERS_PATH", str(pinned))

        assert resolve_routing_tiers_path() == pinned

    def test_resolver_raises_when_unbound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DELEGATION_ROUTING_TIERS_PATH", raising=False)

        with pytest.raises(
            ValueError, match="DELEGATION_ROUTING_TIERS_PATH is not bound"
        ) as exc_info:
            resolve_routing_tiers_path()

        assert "DELEGATION_ROUTING_TIERS_PATH" in str(exc_info.value)
