# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18626: this repo's base contract must agree with the lab binding table.

WHY THIS TEST IS IN OMNIMARKET AND COULD NOT BE ANYWHERE ELSE.

The delegation contract the runtime reads is rendered at container start from
two halves that live in different repositories and arrive by different routes:

    half                         owner            how it reaches the container
    ------------------------     --------------   ----------------------------
    served_model_id              omnibase_infra   a read-only BIND MOUNT of the
      (via the lane overlay,                      lane overlay from the host
       constrained by the                         clone, advanced by the deploy
       authorized binding table)                  agent's GIT PHASE
    model_name                   omnimarket       BAKED INTO THE IMAGE, swapped
      (the base contract)                         at the end of the build

``render_bifrost_delegation_contract._merge_lane_overlay`` refuses outright when
those two disagree. The refusal is correct: a disagreement means the contract
cannot be rendered truthfully. But because the halves move at different moments,
they can disagree with NO BUILD AT ALL, and the container that next starts does
not route badly -- it fails to start.

That happened on 2026-09-17. ``omnibase_infra#3721`` repointed the binding table
and the overlays to ``Qwen3.8-27B`` and merged; its omnimarket twin did not, and
from the moment a deploy job's git phase advanced the host clone, the dev lane
was one container start away from a runtime that would not come up.

**Neither repository can check this alone.** ``omnibase_infra`` cannot import
``omnimarket`` -- that is the compat -> core -> spi -> infra layering, and
reversing it would be a circular dependency. ``omnimarket`` cannot read the
overlay FILES, because ``omnibase_infra`` does not package ``docker/`` and those
files never reach an installed distribution.

What omnimarket CAN see is both authorities: it installs ``omnibase_infra``, so
``_AUTHORIZED_BINDINGS`` is importable, and it owns the base contract. And
checking against the binding table is EQUIVALENT to checking against every valid
overlay, because ``ModelBifrostLaneBackendBinding`` refuses any overlay whose
``served_model_id`` differs from the table. So an overlay that disagrees with
this test's expectation cannot be loaded in the first place.

WHEN THIS FIRES, which is the useful part. It goes red at the moment the two
halves would actually diverge in an image: the omnimarket bump of its
``omnibase-infra`` floor. A bump that pulls in a repointed binding table without
the matching base-contract change is refused here, in the repo making the bump,
rather than discovered as a dead lane hours later.

HONEST LIMIT. This is a static check over one installed pair. It cannot observe
the running host, where the mounted overlay may be newer than the baked base.
Closing THAT window needs provenance reporting at render time and is tracked as
the next increment of OMN-18626. What this removes is the authoring mistake --
landing one half of a two-repo change -- which is how the 2026-09-17 window was
opened.
"""

from __future__ import annotations

import importlib.resources
from typing import Final

import pytest
import yaml
from omnibase_infra.runtime.models.model_bifrost_lane_backend_binding import (
    _AUTHORIZED_BINDINGS,
)

pytestmark = pytest.mark.unit

_BASE_CONTRACT_RESOURCE: Final[str] = "configs/bifrost_delegation.yaml"


def _base_contract_model_names() -> dict[str, str | None]:
    """``backend_id -> model_name`` from THIS repo's packaged base contract.

    Read through ``importlib.resources`` rather than by path so the test reads
    the file the renderer will actually resolve, not a copy that happens to sit
    beside the source tree.
    """
    resource = importlib.resources.files("omnimarket").joinpath(_BASE_CONTRACT_RESOURCE)
    contract = yaml.safe_load(resource.read_text(encoding="utf-8"))
    backends = contract.get("backends")
    assert isinstance(backends, list), (
        f"{_BASE_CONTRACT_RESOURCE} does not declare a backends list; the "
        "renderer would refuse this contract outright, so this test has "
        "nothing to compare"
    )
    assert backends, (
        f"{_BASE_CONTRACT_RESOURCE} declares an EMPTY backends list. This test "
        "would then pass vacuously, so it fails closed instead"
    )
    return {
        backend["backend_id"]: backend.get("model_name")
        for backend in backends
        if isinstance(backend, dict) and "backend_id" in backend
    }


def _disagreements(
    base_model_names: dict[str, str | None],
    authorized: dict[str, str],
) -> list[str]:
    """Every backend both sides declare where the names cannot both be true.

    A ``None`` base ``model_name`` is NOT a disagreement: the renderer treats it
    as "the overlay is authoritative" and skips its comparison. That branch is
    deliberate and is the shape the next increment moves to once a runtime
    resolver stands behind it.
    """
    findings: list[str] = []
    for backend_id, served in sorted(authorized.items()):
        if backend_id not in base_model_names:
            continue
        declared = base_model_names[backend_id]
        if declared is None or declared == served:
            continue
        findings.append(
            f"{backend_id}: this repo's base contract says model_name="
            f"{declared!r}, the installed omnibase_infra binding table says "
            f"served_model_id={served!r}"
        )
    return findings


def test_base_contract_agrees_with_the_installed_lab_binding_table() -> None:
    """A half-applied two-repo repoint is refused here, not on the lane."""
    authorized = {
        backend_id: binding.served_model_id
        for backend_id, binding in _AUTHORIZED_BINDINGS.items()
    }
    base_model_names = _base_contract_model_names()

    shared = set(authorized) & set(base_model_names)
    assert shared, (
        "this repo's base contract and the installed omnibase_infra binding "
        "table share no backend id at all, so this test would pass vacuously. "
        f"base declares {sorted(base_model_names)}, the table declares "
        f"{sorted(authorized)}"
    )

    findings = _disagreements(base_model_names, authorized)
    assert not findings, (
        "The delegation contract cannot be rendered from this pair, so the next "
        "container start on any lab lane will FAIL TO START rather than route "
        "badly (render_bifrost_delegation_contract._merge_lane_overlay).\n\n"
        + "\n".join(f"  - {finding}" for finding in findings)
        + "\n\nThis is a two-repo change landed one half at a time. Either move "
        "this repo's model_name to match, in the same window as the "
        "omnibase_infra change, or set it to null so the overlay is "
        "authoritative -- but null is only correct once a runtime resolver "
        "stands behind it (OMN-18626 increment 2)."
    )


def test_the_comparison_is_a_real_comparison() -> None:
    """Positive control on the helper, both directions.

    Without this, a helper that returned an empty list unconditionally would
    make the test above pass forever while checking nothing -- which is the
    exact failure mode this ticket exists to remove, so it gets its own control
    rather than a comment.
    """
    authorized = {"local-coder": "ModelA", "local-ds-v4-flash": "ModelB"}

    agreeing = _disagreements(
        {"local-coder": "ModelA", "local-ds-v4-flash": "ModelB"}, authorized
    )
    assert agreeing == [], "an agreeing pair must produce no finding"

    nulled = _disagreements(
        {"local-coder": None, "local-ds-v4-flash": "ModelB"}, authorized
    )
    assert nulled == [], "a null base model_name defers to the overlay, by design"

    disagreeing = _disagreements(
        {"local-coder": "ModelA", "local-ds-v4-flash": "SomethingElse"}, authorized
    )
    assert len(disagreeing) == 1, "a disagreeing pair must produce exactly one finding"
    assert "local-ds-v4-flash" in disagreeing[0]
    assert "SomethingElse" in disagreeing[0]
    assert "ModelB" in disagreeing[0], (
        "the finding must print BOTH values; a message naming only one leaves "
        "the reader unable to tell which half is ahead"
    )

    absent = _disagreements({"a-backend-the-table-does-not-know": "X"}, authorized)
    assert absent == [], "a backend only one side declares is not a disagreement"
