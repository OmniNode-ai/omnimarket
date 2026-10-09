# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden host surface decisions and strict fact/model boundaries."""

from typing import Literal

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_reconcile import (
    ModelGatePurityFact,
    ModelGuardFact,
    ModelHostReconcileEvaluateRequest,
    ModelPathShadowFact,
    ModelSurfaceFact,
    ModelSurfaceIndeterminateFact,
    ModelSurfaceMovementFact,
    ModelSurfaceUncoveredFact,
    ModelSurfaceUnhealthyFact,
)
from omnimarket.nodes.node_host_reconcile_compute.handlers.handler_host_reconcile_compute import (
    HandlerHostReconcileCompute,
)

pytestmark = pytest.mark.unit


def request(
    *facts: ModelSurfaceFact, mode: Literal["check", "repair"] = "repair"
) -> ModelHostReconcileEvaluateRequest:
    return ModelHostReconcileEvaluateRequest(
        mode=mode,
        workspace_root="/workspace",
        scripts_dir="/scripts",
        rerun_command="rerun",
        receipt_path="/receipt",
        facts=facts,
    )


@pytest.mark.parametrize(
    ("before", "after", "target", "verdict", "detail"),
    [
        (
            "a",
            "",
            "b",
            "INDETERMINATE",
            "post-reconcile state is unreadable; refusing to assume it is correct",
        ),
        (
            "a",
            "b",
            "",
            "INDETERMINATE",
            "no target to compare against; a surface with no target cannot be attested",
        ),
        (
            "a",
            "a",
            "b",
            "DID_NOT_MOVE",
            "observed a but target is b (unchanged from a)",
        ),
        ("a", "c", "b", "DID_NOT_MOVE", "observed c but target is b"),
        ("b", "b", "b", "ALREADY_AT_TARGET", "already at b"),
        ("a", "b", "b", "MOVED", "a -> b"),
        ("", "b", "b", "MOVED", "<absent> -> b"),
        (
            "",
            "",
            "",
            "INDETERMINATE",
            "post-reconcile state is unreadable; refusing to assume it is correct",
        ),
    ],
)
def test_movement(
    before: str, after: str, target: str, verdict: str, detail: str
) -> None:
    decision = (
        HandlerHostReconcileCompute()
        .handle(
            request(
                ModelSurfaceMovementFact(
                    surface="clone:omnimarket",
                    before=before,
                    after=after,
                    target=target,
                )
            )
        )
        .surfaces[0]
    )
    assert (decision.verdict, decision.detail) == (verdict, detail)
    if verdict in {"MOVED", "ALREADY_AT_TARGET"}:
        assert decision.remedy == ""


@pytest.mark.parametrize(
    ("fact", "verdict", "detail"),
    [
        (
            ModelSurfaceUnhealthyFact(surface="clone:repo", reason="broken index"),
            "UNHEALTHY",
            "broken index",
        ),
        (
            ModelSurfaceUncoveredFact(
                surface="clone-surface", delegate="/delegate", layer="clone"
            ),
            "UNCOVERED",
            "no clone reconciler at /delegate — the deploy-source clones on this host are reconciled by nobody",
        ),
        (
            ModelSurfaceUncoveredFact(
                surface="venv-surface", delegate="/delegate", layer="venv"
            ),
            "UNCOVERED",
            "no venv reconciler at /delegate — the installed layers on this host are reconciled by nobody",
        ),
        (
            ModelSurfaceIndeterminateFact(surface="venv:dispatch", detail="unreadable"),
            "INDETERMINATE",
            "unreadable",
        ),
        (
            ModelGatePurityFact(
                gate_venv="/gate",
                provider="omnimarket-1.dist-info",
                dispatch_venv="/dispatch",
            ),
            "IMPURE",
            "omnimarket-1.dist-info is installed in /gate, which is lock-governed only — every `uv run pytest` in /workspace/omnibase_infra is refused by the OMN-15620 purity gate while it is there; the provider layer belongs in /dispatch (OMN-17819)",
        ),
        (
            ModelGatePurityFact(gate_venv="/gate", dispatch_venv="/dispatch"),
            "ALREADY_AT_TARGET",
            "no undeclared omnimarket provider in /gate",
        ),
        (
            ModelPathShadowFact(
                shadow="/shadow",
                wrapper="/wrapper",
                wrapper_resolved="/canonical",
                is_symlink=True,
                resolved="/canonical",
                points_to_wrapper=True,
            ),
            "ALREADY_AT_TARGET",
            "/shadow is a symlink to the canonical wrapper (/canonical) — not a shadow",
        ),
        (
            ModelPathShadowFact(
                shadow="/shadow",
                wrapper="/wrapper",
                wrapper_resolved="/canonical",
                is_symlink=True,
                resolved="/elsewhere",
                points_to_wrapper=False,
            ),
            "SHADOWED",
            "/shadow exists (a symlink to /elsewhere) and outranks /wrapper for every non-interactive onex invocation (the interactive-shell alias never covers those) — fix: uv tool uninstall omnibase-core",
        ),
        (
            ModelPathShadowFact(
                shadow="/shadow",
                wrapper="/wrapper",
                wrapper_resolved="/canonical",
                is_symlink=False,
                resolved="/shadow",
                points_to_wrapper=False,
            ),
            "SHADOWED",
            "/shadow exists (a regular file) and outranks /wrapper for every non-interactive onex invocation (the interactive-shell alias never covers those) — fix: uv tool uninstall omnibase-core",
        ),
        (
            ModelGuardFact(
                live_dir="/live",
                source_dir="/source",
                checked=3,
                drifted=("first.sh", "last.sh"),
            ),
            "DRIFT",
            "the installed canonical-clone guard at /live differs from the tracked source at /source: first.sh last.sh -- a guard fix merged to dev is not in force on this host until it is installed (OMN-17291)",
        ),
        (
            ModelGuardFact(
                live_dir="/live", source_dir="/source", checked=3, drifted=()
            ),
            "ALREADY_AT_TARGET",
            "3 installed guard script(s) in /live are byte-identical to /source",
        ),
    ],
)
def test_surface_rules(fact: ModelSurfaceFact, verdict: str, detail: str) -> None:
    row = HandlerHostReconcileCompute().handle(request(fact)).surfaces[0]
    assert (row.verdict, row.detail) == (verdict, detail)


@pytest.mark.parametrize(
    ("surface", "premise"),
    [
        ("clone:omnibase_infra", True),
        ("clone:omnimarket", True),
        ("clone:other", False),
        ("clone-surface", True),
        ("venv-surface", True),
        ("venv:dispatch", True),
        ("venv:package", True),
        ("venv:omnimarket", True),
        ("venv:gate-purity", False),
        ("canonical-guard", False),
        ("onex-path-shadow", False),
        ("other", False),
    ],
)
def test_premise(surface: str, premise: bool) -> None:
    row = (
        HandlerHostReconcileCompute()
        .handle(
            request(ModelSurfaceIndeterminateFact(surface=surface, detail="unknown"))
        )
        .surfaces[0]
    )
    assert row.dispatch_premise is premise


@pytest.mark.parametrize(
    ("surface", "unhealthy", "remedy"),
    [
        ("clone:repo", True, "apply the repair named in the detail, then rerun"),
        (
            "clone:repo",
            False,
            "bash /workspace/omniclaude/scripts/converge-canonical-clone.sh repo --execute, then rerun",
        ),
        (
            "venv:package",
            False,
            "bash /scripts/reconcile-workspace-venvs.sh --omni-home /workspace, then rerun",
        ),
        (
            "venv:gate-purity",
            False,
            "bash /scripts/reconcile-workspace-venvs.sh --omni-home /workspace, then rerun",
        ),
        (
            "clone-surface",
            False,
            "restore the missing delegate named in the detail, then rerun",
        ),
        (
            "venv-surface",
            False,
            "restore the missing delegate named in the detail, then rerun",
        ),
        ("onex-path-shadow", False, "uv tool uninstall omnibase-core, then rerun"),
        (
            "canonical-guard",
            False,
            "bash /scripts/install-canonical-clone-git-hooks.sh (readback), then bash /scripts/install-canonical-clone-git-hooks.sh --apply <the clones the readback lists as ok>, then rerun",
        ),
        ("other", False, "rerun"),
    ],
)
def test_remedies(surface: str, unhealthy: bool, remedy: str) -> None:
    fact = (
        ModelSurfaceUnhealthyFact(surface=surface, reason="bad")
        if unhealthy
        else ModelSurfaceIndeterminateFact(surface=surface, detail="bad")
    )
    assert (
        HandlerHostReconcileCompute().handle(request(fact)).surfaces[0].remedy == remedy
    )


@pytest.mark.parametrize("mode", ["check", "repair"])
@pytest.mark.parametrize("surface", [None, "venv:dispatch", "other"])
def test_exit_and_floor(mode: Literal["check", "repair"], surface: str | None) -> None:
    facts = (
        ()
        if surface is None
        else (ModelSurfaceIndeterminateFact(surface=surface, detail="bad"),)
    )
    result = HandlerHostReconcileCompute().handle(request(*facts, mode=mode))
    assert result.failures == (0 if surface is None else 1)
    assert result.dispatch_premise_failures == (1 if surface == "venv:dispatch" else 0)
    assert result.exit_code == (0 if surface is None else 2)
    assert result.stamp_floor is (mode == "repair" and surface != "venv:dispatch")


@pytest.mark.parametrize("mode", ["check", "repair"])
def test_success_lines(mode: Literal["check", "repair"]) -> None:
    result = HandlerHostReconcileCompute().handle(request(mode=mode))
    assert result.verdict_lines == (
        (
            "VERDICT: IN_SYNC (check mode; nothing mutated, floor untouched)"
            if mode == "check"
            else "VERDICT: IN_SYNC — every surface proven at target."
        ),
    )


@pytest.mark.parametrize("mode", ["check", "repair"])
@pytest.mark.parametrize("surface", ["venv:dispatch", "other"])
def test_failed_lines(mode: Literal["check", "repair"], surface: str) -> None:
    result = HandlerHostReconcileCompute().handle(
        request(ModelSurfaceIndeterminateFact(surface=surface, detail="bad"), mode=mode)
    )
    prefix = (
        (
            "The dispatch premise IS proven; the unrelated failures do not block onex delegate.",
        )
        if mode == "repair" and surface == "other"
        else (
            "The floor marker was NOT stamped; the previous proven floor is retained.",
        )
    )
    if surface == "venv:dispatch":
        prefix += ("blocks onex delegate: venv:dispatch: INDETERMINATE",)
    remedy = (
        "bash /scripts/reconcile-workspace-venvs.sh --omni-home /workspace, then rerun"
        if surface == "venv:dispatch"
        else "rerun"
    )
    assert result.verdict_lines == (
        *prefix,
        "VERDICT: FAILED — 1 surface(s) could not be proven at target.",
        "  receipt: /receipt",
        f"  {surface}: INDETERMINATE — bad",
        f"    clears with: {remedy}",
    )


def test_fact_order_owner_and_receipt_field_order() -> None:
    facts = tuple(
        ModelSurfaceIndeterminateFact(surface=surface, detail="bad", owner="lane")
        for surface in ("z", "venv:dispatch", "a")
    )
    result = HandlerHostReconcileCompute().handle(request(*facts))
    assert tuple(row.surface for row in result.surfaces) == tuple(
        f.surface for f in facts
    )
    assert all(row.owner == "lane" for row in result.surfaces)
    assert list(result.surfaces[0].model_dump()) == [
        "surface",
        "verdict",
        "dispatch_premise",
        "remedy",
        "owner",
        "detail",
    ]


@pytest.mark.parametrize(
    "fact",
    [
        {"kind": "unknown", "surface": "a"},
        {"kind": "movement", "surface": "a", "extra": True},
    ],
)
def test_invalid_fact_error_chain(fact: dict[str, object]) -> None:
    payload = request().model_dump() | {"facts": [fact]}
    with pytest.raises(ValidationError) as exc:
        ModelHostReconcileEvaluateRequest.model_validate(payload)
    assert exc.value.errors()[0]["loc"][0] == "facts"
